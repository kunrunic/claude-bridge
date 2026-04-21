import { randomUUID } from "node:crypto";
import { join } from "node:path";
import { mkdirSync } from "node:fs";
import { loadConfig } from "./channels/telegram/config.ts";
import { paths, CB_INSTANCE } from "./core/paths.ts";
import { TelegramClient } from "./channels/telegram/client.ts";
import { Poller } from "./channels/telegram/poller.ts";
import { Registry } from "./core/registry.ts";
import * as slash from "./core/slash.ts";
import * as tmux from "./core/tmux/session.ts";
import {
  DEFAULT_SOCKET_PATH,
  startServer,
  type IpcMessage,
  LineSocket,
} from "./core/ipc.ts";
import {
  buildCompactKeyboard,
  formatCompactPrompt,
  pendingPermissions,
} from "./channels/telegram/permissions.ts";
import * as anomaly from "./core/anomaly.ts";
import { updateActivePin } from "./core/pin.ts";
import {
  handleIpcHello,
  handleIpcSignal,
  handleIpcPermissionRequest,
  handleInbound,
  deliverPermissionReply as deliverPermissionReplyFn,
} from "./core/dispatcher-handlers.ts";
import {
  acquirePollingLock,
  installShutdownHandlers,
  releasePollingLock,
} from "./core/lifecycle.ts";
import { writeChannelPromptFile, writeMcpConfigFile } from "./core/channel-prompt.ts";
import * as core from "./core/dispatcher-core.ts";
import { SessionManager } from "./core/SessionManager.ts";
import { TickObserver } from "./core/TickObserver.ts";
import { SlashHandler } from "./channels/telegram/SlashHandler.ts";

const CHANNEL_NAME = "tg_channel";
const TMUX_SESSION_PREFIX = CB_INSTANCE ? `cb-${CB_INSTANCE}-` : "cb-";
const BLOCKED_TOOLS = [
  "mcp__plugin_telegram_telegram__reply",
  "mcp__plugin_telegram_telegram__react",
  "mcp__plugin_telegram_telegram__edit_message",
  "mcp__plugin_telegram_telegram__download_attachment",
];
const ALLOWED_TOOLS = [
  "mcp__tg_channel__reply",
  "mcp__tg_channel__react",
  "mcp__tg_channel__edit_message",
  "mcp__tg_channel__download_attachment",
];

function reconcileOrphans(registry: Registry): void {
  for (const s of registry.list()) {
    if (!tmux.hasSession(s.tmuxName)) {
      anomaly.log("orphan_detected", {
        where: "startup",
        kind: "stale_registry_entry",
        sessionId: s.id,
        tmuxName: s.tmuxName,
      });
      registry.remove(s.id);
    }
  }
  const known = new Set(registry.list().map((s) => s.tmuxName));
  for (const name of tmux.listSessions(TMUX_SESSION_PREFIX)) {
    if (known.has(name)) continue;
    anomaly.log("orphan_detected", {
      where: "startup",
      kind: "unknown_tmux",
      tmuxName: name,
    });
    try {
      tmux.killSession(name);
    } catch {
      // best-effort; if kill fails the next startup will see it again
    }
  }
  // after reconciliation, every live session's MCP pipe is freshly opened
  // once Claude reconnects. in practice we killed orphans above, so none
  // should remain attached — but we clear the registry defensively to
  // surface them via the new-session flow.
  for (const s of [...registry.list()]) {
    registry.remove(s.id);
  }
}

async function main(): Promise<void> {
  const config = loadConfig();
  const tg = new TelegramClient(config.botToken);
  const registry = new Registry();
  registry.loadFrom(paths.registryPath);
  const socketPath = process.env.CB_DISPATCHER_SOCKET ?? DEFAULT_SOCKET_PATH;
  const botWorkspaceDir = join(paths.workspacesRoot, "bot");
  mkdirSync(botWorkspaceDir, { recursive: true });

  writeChannelPromptFile(paths.channelPromptFile);
  const serverAbs = writeMcpConfigFile(paths.mcpConfigFile, CHANNEL_NAME);
  anomaly.log("server_startup", {
    where: "dispatcher.main",
    mcpConfigFile: paths.mcpConfigFile,
    serverAbs,
  });
  reconcileOrphans(registry);
  registry.setPersistPath(paths.registryPath);
  registry.setTmuxPrefix(TMUX_SESSION_PREFIX);
  registry.resetSeq();

  const sockets = new Map<string, LineSocket>();
  const permissionToSession = new Map<string, string>();

  function announce(text: string): void {
    if (!config.defaultChatId) return;
    void tg.sendMessage(config.defaultChatId, text).catch((err) => {
      anomaly.log("channel_reply_failed", {
        op: "dispatcher.announce",
        error: String(err),
      });
    });
  }

  const doUpdateActivePin = (): Promise<void> => {
    if (!config.defaultChatId) return Promise.resolve();
    return updateActivePin(registry, tg, config.defaultChatId);
  };

  const spawnCfg: core.SpawnConfig = {
    channelName: CHANNEL_NAME,
    blockedTools: BLOCKED_TOOLS,
    allowedTools: ALLOWED_TOOLS,
    socketPath,
    botWorkspaceDir,
    skipPermissions: config.skipPermissions,
    channelPromptFile: paths.channelPromptFile,
    mcpConfigFile: paths.mcpConfigFile,
  };

  const sessions = new SessionManager({
    registry,
    tmux,
    sockets,
    spawnCfg,
    announce,
  });
  const BUSY_FRAMES = ["🤔", "💭", "🧐", "🤓", "💡", "🤯"];
  const ANIM_TICK_MS = 5_000;
  let animMsgId: number | undefined;
  let animFrame = 0;
  let animTimer: ReturnType<typeof setInterval> | undefined;

  function startAnimation(): void {
    if (!config.defaultChatId) return;
    if (animMsgId !== undefined || animTimer !== undefined) return;
    animFrame = 0;
    const firstEmoji = BUSY_FRAMES[0]!;
    void tg.sendMessage(config.defaultChatId, firstEmoji)
      .then((id) => { animMsgId = id; })
      .catch(() => {});
    animTimer = setInterval(() => {
      if (!config.defaultChatId || animMsgId === undefined) return;
      animFrame++;
      const emoji = BUSY_FRAMES[animFrame % BUSY_FRAMES.length]!;
      void tg.bot.api
        .editMessageText(config.defaultChatId, animMsgId, emoji)
        .catch(() => {});
    }, ANIM_TICK_MS);
  }

  function onReplySent(): void {
    if (animTimer !== undefined) {
      clearInterval(animTimer);
      animTimer = undefined;
    }
    if (!config.defaultChatId || animMsgId === undefined) return;
    const msgId = animMsgId;
    animMsgId = undefined;
    animFrame = 0;
    void tg.bot.api
      .editMessageText(config.defaultChatId, msgId, "✅")
      .then(() => new Promise<void>((r) => setTimeout(r, 5000)))
      .then(() => tg.bot.api.deleteMessage(config.defaultChatId!, msgId))
      .catch(() => {});
  }

  const tick = new TickObserver({
    registry,
    tmux,
    announce,
  });

  function renderStatus(): string {
    const WINDOW_MS = 24 * 60 * 60 * 1000;
    const s = anomaly.summary(WINDOW_MS);
    if (s.total === 0) {
      return "📊 24h anomalies: none\nlog: " + anomaly.LOG_FILE_PATH;
    }
    const ordered = [...s.byKind.entries()].sort((a, b) => b[1] - a[1]);
    const lines: string[] = [`📊 24h anomalies: ${s.total}`];
    for (const [kind, count] of ordered) {
      const badge = kind === "token_collision_detected" ? "⚠️ " : "  ";
      const last = s.lastTs.get(kind)?.slice(11, 19) ?? "";
      lines.push(`${badge}${kind}: ${count}  (last ${last})`);
    }
    lines.push(`log: ${anomaly.LOG_FILE_PATH}`, "", "help:", slash.help());
    return lines.join("\n");
  }

  const slashHandler = new SlashHandler({
    registry,
    tg,
    sessions,
    doUpdateActivePin,
    renderStatus,
  });

  const ipcServer = startServer(socketPath, (ls) => {
    const socketId = randomUUID();
    sockets.set(socketId, ls);
    ls.onMessage((msg: IpcMessage) => {
      switch (msg.op) {
        case "hello": {
          anomaly.log("ipc_hello_received", {
            where: "dispatcher.ipc",
            sessionId: msg.session_id,
            socketId,
          });
          const ready = handleIpcHello(registry, msg.session_id, socketId);
          if (ready?.kind === "spawned") announce(`✅ [${ready.label}] 준비됨 — 메시지를 보내세요`);
          if (ready?.kind === "reconnected") announce(`🔄 [${ready.label}] 재연결됨`);
          break;
        }
        case "permission_request":
          handleIpcPermissionRequest(
            registry,
            permissionToSession,
            pendingPermissions,
            tg,
            config.allowlist,
            buildCompactKeyboard,
            formatCompactPrompt,
            msg,
          );
          break;
        case "signal":
          handleIpcSignal(registry, socketId, msg.signal);
          break;
        case "reply_sent":
          onReplySent();
          break;
        default:
          anomaly.log("mcp_unknown_method", {
            where: "dispatcher.ipc",
            op: (msg as { op: string }).op,
          });
      }
    });
    ls.onClose(() => {
      const session = registry.getBySocketId(socketId);
      sockets.delete(socketId);
      registry.detachSocket(socketId);
      if (session && session.state !== "dead") {
        announce(
          `⚠️ [${session.label}] 연결 끊김 — 자동 재연결 시도 중\n` +
          `재연결 실패 시 /new 또는 /resume 으로 새 세션을 시작하세요.`,
        );
      }
    });
  });

  const poller = new Poller(
    tg,
    config,
    async (evt) => {
      const slashCmd = slash.parse(evt.content);
      if (slashCmd) {
        await slashHandler.handle(slashCmd, evt.meta.chat_id);
        return;
      }
      const delivered = handleInbound(registry, sockets, announce, evt);
      if (delivered) {
        startAnimation();
        if (evt.meta.message_id) {
          void tg
            .setReaction(evt.meta.chat_id, Number(evt.meta.message_id), "✍")
            .catch(() => {});
        }
      }
    },
    (requestId, behavior) =>
      deliverPermissionReplyFn(
        permissionToSession,
        pendingPermissions,
        sockets,
        registry,
        requestId,
        behavior,
      ),
    (action, target, chatId, msgId) =>
      slashHandler.onSessionAction(action, target, chatId, msgId),
  );

  tick.start();

  if (process.env.CB_POLL_DISABLED !== "1") {
    await acquirePollingLock("dispatcher.ts");
    await tg.setCommands(slash.BOT_COMMANDS);
    await poller.start();
  }

  void doUpdateActivePin();
  announce(
    `🟢 claude-bridge started${CB_INSTANCE ? ` [${CB_INSTANCE}]` : ""}\n` +
      `/new — 새 세션  /sessions — 세션 목록  /status — 상태`,
  );

  async function shutdown(): Promise<void> {
    if (animTimer !== undefined) {
      clearInterval(animTimer);
      animTimer = undefined;
    }
    tick.stop();
    void poller.stop();
    const stalePin = registry.getActivePin();
    if (stalePin) {
      await tg.unpinMessage(stalePin.chatId, stalePin.messageId).catch(() => {});
      registry.setActivePin(undefined);
    }
    const kills = registry.list().map((s) => sessions.gracefulKill(s.tmuxName));
    await Promise.all(kills);
    for (const ls of sockets.values()) ls.close();
    ipcServer.close();
    for (const s of [...registry.list()]) {
      registry.remove(s.id);
    }
    releasePollingLock();
  }

  installShutdownHandlers(async (reason) => {
    anomaly.log("shutdown", { where: "dispatcher.ts", reason });
    await shutdown();
  });
}

main().catch((err) => {
  anomaly.log("anomaly_self_error", {
    where: "dispatcher.main",
    error: String(err),
  });
  console.error(err);
  process.exit(1);
});
