import { randomUUID } from "node:crypto";
import { dirname, join, resolve } from "node:path";
import { mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { loadConfig } from "./channels/telegram/config.ts";
import { paths, CB_INSTANCE } from "./core/paths.ts";
import { CbMenuSupervisor } from "./core/cbMenu.ts";
import { TelegramClient } from "./channels/telegram/client.ts";
import { TelegramChannel } from "./channels/telegram/channel.ts";
import { TmuxStatusChannel } from "./channels/tmux/channel.ts";
import { Registry } from "./core/registry.ts";
import * as tmux from "./core/tmux/session.ts";
import {
  DEFAULT_SOCKET_PATH,
  startServer,
  type IpcMessage,
  LineSocket,
} from "./core/ipc.ts";
import * as anomaly from "./core/anomaly.ts";
import {
  handleIpcHello,
  handleIpcSignal,
  handleInbound,
  deliverPermissionReply as deliverPermissionReplyFn,
} from "./core/dispatcher-handlers.ts";
import { handleCliRequest } from "./core/dispatcher-cli-handlers.ts";
import { installShutdownHandlers } from "./core/lifecycle.ts";
import { writeChannelPromptFile, writeMcpConfigFile } from "./core/channel-prompt.ts";
import * as core from "./core/dispatcher-core.ts";
import { SessionManager } from "./core/SessionManager.ts";
import { TickObserver } from "./core/TickObserver.ts";
import { pendingPermissions } from "./channels/telegram/permissions.ts";
import type { Channel, SessionEvent } from "./core/channel.ts";

const CHANNEL_NAME = "bridge-channel";
const TMUX_SESSION_PREFIX = CB_INSTANCE ? `cb-${CB_INSTANCE}-` : "cb-";
// cb-menu — ssh 진입자가 attach 하는 영속 메뉴 session. CbMenuSupervisor 가 관리.
// reconcileOrphans 가 prefix 매치로 모든 cb-* 세션을 죽이므로 startup 마다
// supervisor 가 재생성. 사용자 cb-claude-* 와 한 패밀리지만 lifecycle 은 분리.
const MENU_TMUX_NAME = `${TMUX_SESSION_PREFIX}menu`;
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const MENU_ENTRY = resolve(REPO_ROOT, "src/channels/cli/menu/index.tsx");
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
  // botToken 없으면 CLI-only 모드 — TelegramClient / TelegramChannel 생성 자체를
  // 건너뛴다. 사용자는 cb CLI 로만 dispatcher 와 통신.
  const tg = config.botToken ? new TelegramClient(config.botToken) : undefined;
  const registry = new Registry();
  registry.loadFrom(paths.registryPath);
  const socketPath = process.env.CB_DISPATCHER_SOCKET ?? DEFAULT_SOCKET_PATH;
  const botWorkspaceDir = join(paths.workspacesRoot, "bot");
  mkdirSync(botWorkspaceDir, { recursive: true });

  // bridge 모드 전용 자원 — botToken 있을 때만 prompt / MCP 설정 파일 작성.
  // native 모드면 Claude 가 channel system prompt / MCP server 없이 단순 spawn.
  const mode: core.SpawnMode = config.botToken ? "bridge" : "native";
  if (mode === "bridge") {
    writeChannelPromptFile(paths.channelPromptFile);
    const serverAbs = writeMcpConfigFile(paths.mcpConfigFile, CHANNEL_NAME);
    anomaly.log("server_startup", {
      where: "dispatcher.main",
      mode,
      mcpConfigFile: paths.mcpConfigFile,
      serverAbs,
    });
  } else {
    anomaly.log("server_startup", {
      where: "dispatcher.main",
      mode,
      note: "CLI-only — bridge MCP not registered",
    });
  }
  reconcileOrphans(registry);

  registry.setPersistPath(paths.registryPath);
  registry.setTmuxPrefix(TMUX_SESSION_PREFIX);
  registry.resetSeq();

  const sockets = new Map<string, LineSocket>();
  // 권한 요청 ID → 발생한 세션 ID. 사용자 응답을 IPC 로 라우팅할 때 필요.
  const permissionToSession = new Map<string, string>();

  // ── Channels: forward declaration ─────────────────────────────────────────
  // 채널 인스턴스는 announce / notifyAll 보다 뒤에 만들어진다 (SessionManager
  // 등 일부 의존성이 announce 를 받기 때문). let 으로 선언 후 채워 넣는다.
  let channels: Channel[] = [];

  /** 시스템 안내 텍스트를 모든 채널에 fan-out. */
  function announce(text: string): void {
    for (const ch of channels) ch.announce(text);
  }

  /** SessionEvent 를 모든 채널에 fan-out. */
  function notifyAll(evt: SessionEvent): void {
    for (const ch of channels) ch.notify(evt);
  }

  // MCP 측에 "당신이 active 인가" 정보 전송. 채널 무관 — IPC 책임.
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

  // ── Channel callbacks ─────────────────────────────────────────────────────

  // 채널이 사용자 응답으로 권한 결정을 받으면 호출. dispatcher 가 IPC 로 라우팅.
  function onPermissionReply(requestId: string, behavior: "allow" | "deny"): void {
    deliverPermissionReplyFn(
      permissionToSession,
      pendingPermissions,
      sockets,
      registry,
      requestId,
      behavior,
    );
  }

  // 채널이 사용자로부터 일반 메시지(슬래시 아닌)를 받으면 호출.
  // dispatcher 가 active session 으로 IPC 전달, 성공 시 inbound_delivered 이벤트
  // 발행 (각 채널이 자기 방식으로 ack — Telegram: animation + reaction, etc.).
  async function onInbound(evt: { content: string; meta: Record<string, string | undefined> }): Promise<void> {
    const delivered = handleInbound(registry, sockets, announce, evt);
    if (!delivered) return;
    const active = registry.active();
    if (active) {
      notifyAll({
        type: "inbound_delivered",
        sessionId: active.id,
        label: active.label,
        meta: evt.meta,
      });
    }
  }

  // 채널 내부에서 active session 이 변경된 직후 호출. dispatcher 가 모든
  // 채널에 active_changed / active_cleared fan-out + IPC session_state 갱신.
  function onActiveChangedRequest(previousId: string | undefined): void {
    const active = registry.active();
    if (active) {
      notifyAll({
        type: "active_changed",
        sessionId: active.id,
        label: active.label,
        ...(previousId !== undefined ? { previousId } : {}),
      });
    } else {
      notifyAll({
        type: "active_cleared",
        ...(previousId !== undefined ? { previousId } : {}),
      });
    }
    pushSessionStateToAll();
  }

  /**
   * 세션이 ready 상태가 됐을 때 dispatcher 측 후처리.
   *  - bridge 모드: hello IPC 받고 호출
   *  - native 모드: spawn 직후 SessionManager.onSpawnFinalized 가 호출
   * 두 경로가 같은 책임 (active 전환, SessionEvent fan-out, IPC push) 을 갖도록 통합.
   */
  function onSessionReady(sessionId: string, kind: "spawned" | "reconnected"): void {
    const s = registry.get(sessionId);
    if (!s) return;
    const beforeActiveId = registry.active()?.id;
    if (kind === "spawned") {
      if (!s.noAutoSwitch) {
        registry.setActive(sessionId);
      }
      notifyAll({
        type: "spawned",
        sessionId,
        label: s.label,
        resumed: !!s.source,
        autoSwitched: !s.noAutoSwitch,
      });
      if (!s.noAutoSwitch && beforeActiveId !== sessionId) {
        onActiveChangedRequest(beforeActiveId);
      } else {
        pushSessionState(sessionId);
      }
    } else {
      notifyAll({ type: "reconnected", sessionId, label: s.label });
      pushSessionState(sessionId);
    }
  }

  // ── SessionManager / TickObserver ─────────────────────────────────────────

  const spawnCfg: core.SpawnConfig = {
    mode,
    socketPath,
    botWorkspaceDir,
    skipPermissions: config.skipPermissions,
    menuTmuxName: MENU_TMUX_NAME,
    ...(mode === "bridge"
      ? {
          channelName: CHANNEL_NAME,
          blockedTools: BLOCKED_TOOLS,
          allowedTools: ALLOWED_TOOLS,
          channelPromptFile: paths.channelPromptFile,
          mcpConfigFile: paths.mcpConfigFile,
        }
      : {}),
  };

  const sessions = new SessionManager({
    registry,
    tmux,
    sockets,
    spawnCfg,
    announce,
    // native 모드에서 spawn 직후 즉시 ready 처리. bridge 모드면 사용 안 함.
    onSpawnFinalized: (sid) => onSessionReady(sid, "spawned"),
  });

  const tick = new TickObserver({
    registry,
    tmux,
    announce,
  });

  // ── Channels: instantiate ─────────────────────────────────────────────────

  // Telegram 채널은 botToken 있을 때만. 없으면 CLI-only 모드.
  const telegramChannel = tg
    ? new TelegramChannel({
        tg,
        config,
        registry,
        sessions,
        onInbound,
        onPermissionReply,
        onActiveChangedRequest,
      })
    : undefined;
  // tmux status bar 채널은 입력이 없는 출력 전용. onInbound / onPermissionReply
  // 콜백은 인터페이스 일치를 위해 동일 핸들러 공유 (실제 호출되지 않음).
  const tmuxChannel = new TmuxStatusChannel({
    registry,
    onInbound,
    onPermissionReply,
  });
  channels = telegramChannel ? [telegramChannel, tmuxChannel] : [tmuxChannel];

  if (telegramChannel) {
    // 이전 실행에서 영속된 reply pin 메시지 ID 들을 메모리에 복원 후, stale 한 핀을
    // 정리한다. defaultChatId 가 없으면 채널 내부에서 no-op.
    telegramChannel.loadPersistedPinnedReplies();
    await telegramChannel.cleanupStalePins();
  }

  // ── IPC server ────────────────────────────────────────────────────────────

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
          if (ready) {
            onSessionReady(ready.id, ready.kind);
          } else {
            // unknown session — registry 에 없는 소켓이면 그냥 현재 상태만 push.
            pushSessionState(msg.session_id);
          }
          break;
        }
        case "permission_request": {
          permissionToSession.set(msg.request_id, msg.session_id);
          const isActive = registry.active()?.id === msg.session_id;
          for (const ch of channels) {
            // Telegram에는 active 세션의 권한 요청만 전달.
            // handoff 후 active가 되면 자동으로 Telegram으로 흐름.
            if (!isActive && ch === telegramChannel) continue;
            ch.requestPermission({
              requestId: msg.request_id,
              sessionId: msg.session_id,
              toolName: msg.tool_name,
              description: msg.description,
              inputPreview: msg.input_preview,
            });
          }
          break;
        }
        case "signal": {
          handleIpcSignal(registry, socketId, msg.signal);
          const session = registry.getBySocketId(socketId);
          if (session) {
            notifyAll({
              type: "state_changed",
              sessionId: session.id,
              label: session.label,
              state: session.state,
              signal: session.signal,
            });
          }
          break;
        }
        case "reply_sent": {
          const session = registry.getBySocketId(socketId);
          if (session) {
            registry.updateState(session.id, { replySentAt: Date.now() });
            notifyAll({
              type: "reply_sent",
              sessionId: session.id,
              label: session.label,
              ...(msg.message_ids !== undefined ? { messageIds: msg.message_ids } : {}),
            });
          }
          break;
        }
        case "set_active_request": {
          const target = registry.get(msg.session_id);
          if (!target) {
            anomaly.log("set_active_not_found", { sessionId: msg.session_id });
            break;
          }
          const prev = registry.active()?.id;
          registry.setActive(msg.session_id);
          // noAutoSwitch=false 로 reset — Telegram 이 control 을 가져갔으므로
          // 라벨이 [Telegram] 으로 전환되도록 (ownership 모델 일관성).
          registry.updateState(msg.session_id, { noAutoSwitch: false });
          announce(`🔀 handoff: [${target.id}][${target.label}] → Telegram active`);
          onActiveChangedRequest(prev);
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
        case "cli_request": {
          // cb CLI 요청. 동일 소켓에 MCP 와 공존 — op 로 구분.
          // 같은 cli connection 은 응답 1회 후 close (LineSocket 그대로 유지).
          const resp = handleCliRequest(msg, {
            registry,
            sessions,
            announce,
            onActiveChangedRequest,
          });
          ls.send(resp);
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
      const wasActive = session && registry.active()?.id === session.id;
      sockets.delete(socketId);
      registry.detachSocket(socketId);  // state → dead (mutates session object)
      if (session) {
        if (wasActive) {
          // active 세션이 죽으면 active 포인터를 지우고 Telegram 핀 정리.
          registry.clearActive();
          onActiveChangedRequest(session.id);
        }
        notifyAll({
          type: "disconnected",
          sessionId: session.id,
          label: session.label,
          sshSession: !!session.noAutoSwitch,
        });
      }
    });
  });

  // ── Start ─────────────────────────────────────────────────────────────────

  tick.start();

  // CB_POLL_DISABLED=1 / polling lock 은 TelegramChannel 내부에서 처리한다.
  // 다른 채널 (tmux status, 향후 cli) 은 polling 안 하므로 무관.
  await Promise.all(channels.map((ch) => ch.start()));

  // cb-menu — ssh 진입 시 attach 할 영속 메뉴 session.
  // ipcServer 가 떠 있어야 menu TUI 가 RPC 호출 가능하므로 여기서 시작.
  const cbMenu = new CbMenuSupervisor({
    tmuxName: MENU_TMUX_NAME,
    entry: MENU_ENTRY,
    socketPath,
  });
  cbMenu.start();

  // active session 이 없으면 active_cleared 로 기존 핀 정리. 채널들이 알아서 처리.
  if (!registry.active()) {
    notifyAll({ type: "active_cleared" });
  } else {
    pushSessionStateToAll();
  }

  announce(
    `🟢 claude-bridge started${CB_INSTANCE ? ` [${CB_INSTANCE}]` : ""}\n` +
      `/new — 새 세션  /sessions — 세션 목록 / 전환`,
  );

  // ── Shutdown ──────────────────────────────────────────────────────────────

  async function shutdown(): Promise<void> {
    tick.stop();
    // cb-menu 먼저 정지 — supervisor 폴링 루프 중단 + tmux session 정리.
    cbMenu.stop();
    // 각 채널이 자기 lock / 자원 정리. TelegramChannel 이 내부에서 releasePollingLock.
    await Promise.all(channels.map((ch) => ch.stop()));
    const kills = registry.list().map((s) => sessions.gracefulKill(s.tmuxName));
    await Promise.all(kills);
    for (const ls of sockets.values()) ls.close();
    ipcServer.close();
    for (const s of [...registry.list()]) {
      registry.remove(s.id);
    }
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
