import { randomUUID } from "node:crypto";
import { dirname, join } from "node:path";
import { homedir } from "node:os";
import { mkdirSync } from "node:fs";
import { loadConfig } from "./config.ts";
import { TelegramClient } from "./telegram/client.ts";
import { Poller } from "./telegram/poller.ts";
import { Registry } from "./registry.ts";
import * as slash from "./slash.ts";
import * as tmux from "./tmux/session.ts";
import { observe } from "./observer.ts";
import {
  DEFAULT_SOCKET_PATH,
  startServer,
  type IpcMessage,
  LineSocket,
} from "./ipc.ts";
import {
  buildCompactKeyboard,
  buildExpandedKeyboard,
  formatCompactPrompt,
  formatExpandedBody,
  pendingPermissions,
} from "./permissions.ts";
import * as anomaly from "./anomaly.ts";
import {
  acquirePollingLock,
  installShutdownHandlers,
  releasePollingLock,
} from "./lifecycle.ts";
import * as core from "./dispatcher-core.ts";

const OBSERVE_TICK_MS = 5_000;
const CHANNEL_NAME = "tg_channel";
const DEV_WARNING_POLL_MS = 200;
const DEV_WARNING_TIMEOUT_MS = 10_000;
const DEV_WARNING_MARKER = "WARNING: Loading development channels";
const TRUST_DIALOG_MARKER = "I trust this folder";
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

async function main(): Promise<void> {
  const config = loadConfig();
  const tg = new TelegramClient(config.botToken);
  const registry = new Registry();
  const socketPath = process.env.CB_DISPATCHER_SOCKET ?? DEFAULT_SOCKET_PATH;
  const botWorkspaceDir = join(homedir(), ".claude-bridge", "workspaces", "bot");
  mkdirSync(botWorkspaceDir, { recursive: true });

  const sockets = new Map<string, LineSocket>();
  const permissionToSession = new Map<string, string>();

  const ipcServer = startServer(socketPath, (ls) => {
    const socketId = randomUUID();
    sockets.set(socketId, ls);
    ls.onMessage((msg) => handleIpc(socketId, ls, msg));
    ls.onClose(() => {
      sockets.delete(socketId);
      registry.detachSocket(socketId);
    });
  });

  function announce(text: string): void {
    if (!config.defaultChatId) return;
    void tg.sendMessage(config.defaultChatId, text).catch((err) => {
      anomaly.log("channel_reply_failed", {
        op: "dispatcher.announce",
        error: String(err),
      });
    });
  }

  function handleIpc(socketId: string, ls: LineSocket, msg: IpcMessage): void {
    switch (msg.op) {
      case "hello": {
        registry.attachSocket(msg.session_id, socketId);
        const s = registry.get(msg.session_id);
        if (s) {
          registry.updateState(msg.session_id, { state: "idle" });
        }
        break;
      }
      case "permission_request": {
        permissionToSession.set(msg.request_id, msg.session_id);
        pendingPermissions.set(msg.request_id, {
          tool_name: msg.tool_name,
          description: msg.description,
          input_preview: msg.input_preview,
        });
        const session = registry.get(msg.session_id);
        const label = session ? `[${session.label}] ` : "";
        const keyboard = buildCompactKeyboard(msg.request_id);
        const prompt = `${label}${formatCompactPrompt(msg.tool_name)}`;
        for (const chatId of config.allowlist) {
          void tg.sendWithKeyboard(chatId, prompt, keyboard).catch((err) => {
            anomaly.log("channel_reply_failed", {
              op: "permission_request_relay",
              chatId,
              requestId: msg.request_id,
              error: String(err),
            });
          });
        }
        break;
      }
      case "signal": {
        const session = registry.getBySocketId(socketId);
        if (!session) break;
        registry.updateState(session.id, { signal: msg.signal as never });
        break;
      }
      default:
        anomaly.log("mcp_unknown_method", {
          where: "dispatcher.ipc",
          op: msg.op,
        });
    }
  }

  function deliverPermissionReply(
    requestId: string,
    behavior: "allow" | "deny",
  ): void {
    const sessionId = permissionToSession.get(requestId);
    permissionToSession.delete(requestId);
    pendingPermissions.delete(requestId);
    if (!sessionId) return;
    const session = registry.get(sessionId);
    if (!session || !session.socketId) return;
    const ls = sockets.get(session.socketId);
    if (!ls) return;
    ls.send({ op: "permission_reply", request_id: requestId, behavior });
  }

  function confirmTrustDialog(tmuxName: string, sessionId: string): void {
    const deadline = Date.now() + DEV_WARNING_TIMEOUT_MS;
    const poll = (): void => {
      if (Date.now() > deadline) return;
      let pane = "";
      try { pane = tmux.capturePane(tmuxName, 40); } catch {
        setTimeout(poll, DEV_WARNING_POLL_MS); return;
      }
      if (pane.includes(TRUST_DIALOG_MARKER)) {
        try { tmux.sendKeys(tmuxName, "", true); } catch (err) {
          anomaly.log("session_spawn_failed", { op: "trust-dialog-confirm", session: sessionId, error: String(err) });
        }
        return;
      }
      setTimeout(poll, DEV_WARNING_POLL_MS);
    };
    setTimeout(poll, DEV_WARNING_POLL_MS);
  }

  function confirmDevWarning(tmuxName: string, sessionId: string): void {
    const deadline = Date.now() + DEV_WARNING_TIMEOUT_MS;
    const poll = (): void => {
      if (Date.now() > deadline) {
        anomaly.log("session_spawn_failed", {
          op: "dev-warning-timeout",
          session: sessionId,
          timeoutMs: DEV_WARNING_TIMEOUT_MS,
        });
        return;
      }
      let pane = "";
      try {
        pane = tmux.capturePane(tmuxName, 40);
      } catch {
        setTimeout(poll, DEV_WARNING_POLL_MS);
        return;
      }
      if (pane.includes(DEV_WARNING_MARKER)) {
        try {
          tmux.sendKeys(tmuxName, "", true);
        } catch (err) {
          anomaly.log("session_spawn_failed", {
            op: "dev-warning-confirm",
            session: sessionId,
            error: String(err),
          });
        }
        return;
      }
      setTimeout(poll, DEV_WARNING_POLL_MS);
    };
    setTimeout(poll, DEV_WARNING_POLL_MS);
  }

  const spawnCfg: core.SpawnConfig = {
    channelName: CHANNEL_NAME,
    blockedTools: BLOCKED_TOOLS,
    allowedTools: ALLOWED_TOOLS,
    socketPath,
    botWorkspaceDir,
  };

  const spawnSession = (label?: string) =>
    core.spawnSession(
      {
        registry,
        tmux,
        cfg: spawnCfg,
        onSpawned: (tmuxName, sessionId) => {
          confirmTrustDialog(tmuxName, sessionId);
          confirmDevWarning(tmuxName, sessionId);
        },
      },
      label,
    );

  const killSession = (target: string): boolean =>
    core.killSession({ registry, tmux, sockets }, target);

  const handleSlash = (cmd: slash.SlashCommand, _chatId: string): string =>
    core.handleSlash(
      { registry, spawn: spawnSession, kill: killSession, renderStatus },
      cmd,
    );

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
    lines.push(`log: ${anomaly.LOG_FILE_PATH}`);
    lines.push("", "help:", slash.help());
    return lines.join("\n");
  }

  const poller = new Poller(
    tg,
    config,
    (evt) => {
      const slashCmd = slash.parse(evt.content);
      if (slashCmd) {
        const reply = handleSlash(slashCmd, evt.meta.chat_id);
        void tg.sendMessage(evt.meta.chat_id, reply).catch(() => {});
        return;
      }

      const active = registry.active();
      if (!active || !active.socketId) {
        announce("no active session — use /new to spawn one, or /sessions");
        anomaly.log("inbound_no_active_session", {
          preview: evt.content.slice(0, 40),
        });
        return;
      }
      const ls = sockets.get(active.socketId);
      if (!ls) {
        anomaly.log("inbound_no_active_session", {
          reason: "socket_missing",
          sessionId: active.id,
        });
        return;
      }
      ls.send({
        op: "inbound",
        content: evt.content,
        meta: evt.meta as Record<string, string>,
      });
      registry.pushBacklog(active.id, `← ${evt.content.slice(0, 120)}`);
    },
    (requestId, behavior) => deliverPermissionReply(requestId, behavior),
  );

  const tickTimer = setInterval(() => {
    for (const s of registry.list()) {
      if (s.state === "dead") continue;
      const pane = tmux.capturePane(s.tmuxName, 40);
      if (!pane) continue;
      const obs = observe(pane);
      const patch: { signal: typeof obs.signal; state?: typeof s.state; busySince?: number } = {
        signal: obs.signal,
      };
      if (obs.signal === "busy" || obs.signal === "compact") {
        patch.state = "busy";
        if (!s.busySince) patch.busySince = Date.now();
      } else if (obs.signal === "idle") {
        patch.state = "idle";
      } else if (obs.signal === "compact_error" || obs.signal === "context_limit") {
        patch.state = "error";
      }
      registry.updateState(s.id, patch);
    }
  }, OBSERVE_TICK_MS);

  if (process.env.CB_POLL_DISABLED !== "1") {
    await acquirePollingLock("dispatcher.ts");
    await poller.start();
  }

  function shutdown(): void {
    clearInterval(tickTimer);
    for (const ls of sockets.values()) ls.close();
    ipcServer.close();
    void poller.stop();
    for (const s of registry.list()) {
      tmux.killSession(s.tmuxName);
    }
    releasePollingLock();
  }

  installShutdownHandlers((reason) => {
    anomaly.log("shutdown", { where: "dispatcher.ts", reason });
    shutdown();
  });
}

main().catch((err) => {
  anomaly.log("anomaly_self_error", { where: "dispatcher.main", error: String(err) });
  console.error(err);
  process.exit(1);
});
