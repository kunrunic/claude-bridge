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

const CHANNEL_NAME = "bridge-channel";
const TMUX_SESSION_PREFIX = CB_INSTANCE ? `cb-${CB_INSTANCE}-` : "cb-";
const BLOCKED_TOOLS = [
  "mcp__plugin_telegram_telegram__reply",
  "mcp__plugin_telegram_telegram__react",
  "mcp__plugin_telegram_telegram__edit_message",
  "mcp__plugin_telegram_telegram__download_attachment",
];
const ALLOWED_TOOLS = [
  "mcp__bridge-channel__reply",
  "mcp__bridge-channel__react",
  "mcp__bridge-channel__edit_message",
  "mcp__bridge-channel__download_attachment",
];

function reconcileOrphans(registry: Registry): void {
  // Kill ALL bridge-owned tmux sessions unconditionally on startup.
  // Design: sessions do not survive bridge restarts — clean slate every time.
  // This handles the case where a previous bridge was SIGKILL'd before
  // gracefulKill could finish, leaving tmux sessions alive.
  for (const name of tmux.listSessions(TMUX_SESSION_PREFIX)) {
    anomaly.log("orphan_detected", {
      where: "startup",
      tmuxName: name,
    });
    try {
      tmux.killSession(name);
    } catch {
      // best-effort; if kill fails the next startup will see it again
    }
  }
  for (const s of [...registry.list()]) {
    registry.remove(s.id);
  }
  registry.resetSeq();
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

  // Clean up stale pins from previous run — active pin + persisted reply pins.
  // tg.unpinMessage retries internally; here we only decide what counts as
  // "goal met" so we can clear registry state. "message to unpin not found"
  // means the pin is already gone (manual unpin or message deleted) — still
  // goal met. Any other persistent failure → keep registry entry for next
  // startup to retry.
  if (config.defaultChatId) {
    const chat = config.defaultChatId;
    const isGoalMet = async (messageId: number): Promise<boolean> => {
      try {
        await tg.unpinMessage(chat, messageId);
        return true;
      } catch (err) {
        const msg = String(err).toLowerCase();
        return msg.includes("message to unpin not found") || msg.includes("message not found");
      }
    };

    const staleActivePin = registry.getActivePin();
    if (staleActivePin && await isGoalMet(staleActivePin.messageId)) {
      registry.setActivePin(undefined);
    }
    const stalePinnedReplies = registry.getPinnedReplies();
    const remaining: Record<string, number[]> = {};
    for (const [sid, ids] of Object.entries(stalePinnedReplies)) {
      const stillPinned: number[] = [];
      for (const mid of ids) {
        if (!(await isGoalMet(mid))) stillPinned.push(mid);
      }
      if (stillPinned.length > 0) remaining[sid] = stillPinned;
    }
    registry.setPinnedReplies(remaining);
  }

  registry.setPersistPath(paths.registryPath);
  registry.setTmuxPrefix(TMUX_SESSION_PREFIX);
  registry.resetSeq();

  const sockets = new Map<string, LineSocket>();
  const permissionToSession = new Map<string, string>();

  // Announce a system event. Retry/timeout is handled inside TelegramClient;
  // here we just track the high-level outcome (start/ok/give_up).
  function announce(text: string): void {
    const preview = text.slice(0, 80);
    if (!config.defaultChatId) {
      anomaly.log("dispatcher_announce_skip", { reason: "no_default_chat", preview });
      return;
    }
    anomaly.log("dispatcher_announce_start", { preview });
    void tg.sendMessage(config.defaultChatId, text).then(
      () => {
        anomaly.log("dispatcher_announce_ok", { preview });
      },
      (err) => {
        anomaly.log("dispatcher_announce_give_up", {
          preview,
          error: String(err),
        });
      },
    );
  }

  // Telegram message IDs of inactive-session replies that have been pinned.
  // Unpinned when user leaves that session (or session is killed).
  const pinnedReplyIds = new Map<string, number[]>();

  // Serialize all pin-related Telegram ops through one queue — concurrent
  // calls would race on unpin/pin sequences and leave orphan pins or incorrect
  // ordering in the chat.
  let pinOpsQueue: Promise<void> = Promise.resolve();
  function enqueuePinOp<T>(op: () => Promise<T>, where: string): Promise<T> {
    const p = pinOpsQueue.then(op);
    pinOpsQueue = p.then(
      () => {},
      (err) => {
        anomaly.log("anomaly_self_error", { where, error: String(err) });
      },
    );
    return p;
  }

  const doUpdateActivePin = (): Promise<void> => {
    const chatId = config.defaultChatId;
    if (!chatId) return Promise.resolve();
    return enqueuePinOp(
      () => updateActivePin(registry, tg, chatId),
      "doUpdateActivePin",
    );
  };

  function syncPinnedRepliesToRegistry(): void {
    registry.setPinnedReplies(Object.fromEntries(pinnedReplyIds));
  }

  function pinInactiveReplies(sessionId: string, messageIds: number[]): Promise<void> {
    if (!config.defaultChatId) return Promise.resolve();
    const chat = config.defaultChatId;
    return enqueuePinOp(async () => {
      const existing = pinnedReplyIds.get(sessionId) ?? [];
      for (const mid of messageIds) {
        try {
          await tg.pinMessage(chat, mid, true);
          existing.push(mid);
        } catch (err) {
          anomaly.log("telegram_api_failed", {
            op: "pinInactiveReply",
            sessionId,
            messageId: mid,
            error: String(err),
          });
        }
      }
      pinnedReplyIds.set(sessionId, existing);
      syncPinnedRepliesToRegistry();
    }, "pinInactiveReplies");
  }

  function unpinRepliesOf(sessionId: string): Promise<void> {
    if (!config.defaultChatId) return Promise.resolve();
    const chat = config.defaultChatId;
    const ids = pinnedReplyIds.get(sessionId);
    if (!ids || ids.length === 0) return Promise.resolve();
    pinnedReplyIds.delete(sessionId);
    syncPinnedRepliesToRegistry();
    return enqueuePinOp(async () => {
      for (const mid of ids) {
        try {
          await tg.unpinMessage(chat, mid);
        } catch (err) {
          anomaly.log("telegram_api_failed", {
            op: "unpinReplyOnLeave",
            sessionId,
            messageId: mid,
            error: String(err),
          });
        }
      }
    }, "unpinRepliesOf");
  }

  // After pinning new reply messages, Telegram shows the NEWEST pin at the top
  // of the banner. To keep the active-session summary visible on top, re-pin
  // the summary message after each reply-pin burst (unpin → pin same message
  // On session switch: send a fresh "⚡ [id][label] 활성화" message, pin it,
  // and unpin the old one. This puts the pin at the current chat position
  // so tapping it scrolls to the switch point, not an old location.
  function switchActivePin(id: string, label: string): Promise<void> {
    const chatId = config.defaultChatId;
    if (!chatId) return Promise.resolve();
    return enqueuePinOp(async () => {
      const currentPin = registry.getActivePin();
      let newMsgId: number;
      try {
        newMsgId = await tg.sendMessage(chatId, `⚡ [${id}][${label}] 활성화`);
      } catch (err) {
        anomaly.log("telegram_api_failed", { op: "switchActivePin.send", error: String(err) });
        return;
      }
      try {
        await tg.pinMessage(chatId, newMsgId, true);
        registry.setActivePin({ chatId, messageId: newMsgId });
      } catch (err) {
        anomaly.log("telegram_api_failed", { op: "switchActivePin.pin", error: String(err) });
      }
      if (currentPin) {
        await tg.unpinMessage(currentPin.chatId, currentPin.messageId).catch(() => {});
      }
    }, "switchActivePin");
  }

  function bumpActivePinToTop(): Promise<void> {
    return enqueuePinOp(async () => {
      const pin = registry.getActivePin();
      if (!pin) return;
      const { chatId, messageId } = pin;
      try {
        await tg.unpinMessage(chatId, messageId);
      } catch {
        // already unpinned or gone — swallow and continue to re-pin
      }
      try {
        await tg.pinMessage(chatId, messageId, true);
      } catch (err) {
        anomaly.log("telegram_api_failed", {
          op: "bumpActivePinToTop",
          error: String(err),
        });
      }
    }, "bumpActivePinToTop");
  }

  function pushSessionState(sessionId: string): void {
    const session = registry.get(sessionId);
    if (!session) return;
    if (!session.socketId) return;
    const ls = sockets.get(session.socketId);
    if (!ls) return;
    const active = registry.active();
    ls.send({
      op: "session_state",
      session_id: sessionId,
      is_active: active?.id === sessionId,
      label: session.label,
    });
  }

  function pushSessionStateToAll(): void {
    for (const s of registry.list()) {
      pushSessionState(s.id);
    }
  }

  function onActiveChanged(leftSessionId?: string): void {
    if (leftSessionId) {
      void unpinRepliesOf(leftSessionId);
    }
    const active = registry.active();
    if (active) {
      void switchActivePin(active.id, active.label);
    } else {
      void doUpdateActivePin();
    }
    pushSessionStateToAll();
  }

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

  const slashHandler = new SlashHandler({
    registry,
    tg,
    sessions,
    onActiveChanged,
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
          const beforeActiveId = registry.active()?.id;
          const ready = handleIpcHello(registry, msg.session_id, socketId);
          if (ready?.kind === "spawned") {
            // Auto-switch to newly spawned session — user's /new implies intent to use it.
            registry.setActive(ready.id);
            const s = registry.get(ready.id);
            const readyLabel = s?.source ? "이어하기 준비됨" : "준비됨";
            announce(`✅ [${ready.id}][${ready.label}] ${readyLabel} — 자동 전환됨`);
            // SlashHandler may have already fired onActiveChanged after spawn
            // (registry.create sets activeId synchronously). Skip duplicate fire.
            if (beforeActiveId !== ready.id) {
              onActiveChanged(beforeActiveId);
            }
          } else if (ready?.kind === "reconnected") {
            announce(`🔄 [${ready.id}][${ready.label}] 재연결됨`);
            pushSessionState(msg.session_id);
          } else {
            pushSessionState(msg.session_id);
          }
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
        case "reply_sent": {
          const session = registry.getBySocketId(socketId);
          const active = registry.active();
          if (session && active?.id !== session.id) {
            if (msg.message_ids && msg.message_ids.length > 0) {
              void pinInactiveReplies(session.id, msg.message_ids);
            }
            void doUpdateActivePin();
            void bumpActivePinToTop();
          }
          onReplySent();
          break;
        }
        case "spawn_request": {
          anomaly.log("ipc_spawn_request", {
            where: "dispatcher.ipc",
            cwd: msg.cwd,
            resumeId: msg.resumeId,
          });
          try {
            const spawnOpts: core.SpawnOptions = { cwd: msg.cwd };
            if (msg.resumeId) spawnOpts.resumeId = msg.resumeId;
            if (msg.skipPermissions) spawnOpts.skipPermissions = msg.skipPermissions;
            const r = sessions.spawn(spawnOpts);
            announce(`🔀 handoff: spawning [${r.id}][${r.label}]${msg.resumeId ? " (resume)" : ""}...`);
          } catch (err) {
            announce(`✗ handoff failed: ${String(err)}`);
          }
          break;
        }
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
      if (session) {
        void unpinRepliesOf(session.id);
        if (session.state !== "dead") {
          announce(
            `⚠️ [${session.id}][${session.label}] 연결 끊김 — 자동 재연결 시도 중\n` +
            `재연결 실패 시 /new 또는 /resume 으로 새 세션을 시작하세요.`,
          );
        }
        void doUpdateActivePin();
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
        const active = registry.active();
        if (active) {
          void tg
            .sendMessage(evt.meta.chat_id, `[${active.id}][${active.label}] 사용중`)
            .catch(() => {});
        }
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
      `/new — 새 세션  /sessions — 세션 목록 / 전환`,
  );

  async function shutdown(): Promise<void> {
    if (animTimer !== undefined) {
      clearInterval(animTimer);
      animTimer = undefined;
    }
    tick.stop();
    void poller.stop();
    // On shutdown we try to unpin, but only drop registry entries if the unpin
    // actually succeeded (or the pin is already gone). If it fails — e.g.
    // network hang — we MUST keep the ID so the next startup can retry,
    // otherwise orphan pins linger in Telegram forever.
    const wasGoalMet = async (msgId: number, chatId: string): Promise<boolean> => {
      try {
        await tg.unpinMessage(chatId, msgId);
        return true;
      } catch (err) {
        const m = String(err).toLowerCase();
        return m.includes("message to unpin not found") || m.includes("message not found");
      }
    };

    const stalePin = registry.getActivePin();
    if (stalePin && await wasGoalMet(stalePin.messageId, stalePin.chatId)) {
      registry.setActivePin(undefined);
    }
    if (config.defaultChatId) {
      const chat = config.defaultChatId;
      const remaining = new Map<string, number[]>();
      for (const [sid, ids] of pinnedReplyIds) {
        const stillPinned: number[] = [];
        for (const mid of ids) {
          if (!(await wasGoalMet(mid, chat))) stillPinned.push(mid);
        }
        if (stillPinned.length > 0) remaining.set(sid, stillPinned);
      }
      pinnedReplyIds.clear();
      for (const [sid, ids] of remaining) pinnedReplyIds.set(sid, ids);
      registry.setPinnedReplies(Object.fromEntries(remaining));
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
