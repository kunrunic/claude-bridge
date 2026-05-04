import type { Registry } from "./registry.ts";
import type { IpcMessage } from "./ipc.ts";
import type { LineSocket } from "./ipc.ts";
import type { InboundEvent } from "./channel.ts";
import * as anomaly from "./anomaly.ts";

// ── Spawn timeout ─────────────────────────────────────────────────────────────

export function scheduleSpawnTimeout(
  registry: Registry,
  sessionId: string,
  label: string,
  timeoutMs: number,
  announce: (text: string) => void,
  onKill?: () => void,
): ReturnType<typeof setTimeout> {
  const timer = setTimeout(() => {
    const s = registry.get(sessionId);
    if (s && s.state === "spawning") {
      registry.updateState(sessionId, { state: "error" });
      announce(`⚠️ [${sessionId}][${label}] 기동 실패 — IPC hello 미수신 (${timeoutMs / 1000}s 초과)`);
      anomaly.log("session_spawn_failed", {
        sessionId,
        label,
        reason: "hello_timeout",
      });
      onKill?.();
    }
  }, timeoutMs);
  timer.unref();
  return timer;
}

export type PermissionInfo = {
  tool_name: string;
  description: string;
  input_preview: string;
};

export type PermissionTelegramDeps = {
  sendWithKeyboard: (
    chatId: string,
    text: string,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    keyboard: any,
  ) => Promise<number>;
};

export type BuildKeyboardFn = (requestId: string) => unknown;
export type FormatPromptFn = (toolName: string) => string;

// ── IPC handlers ──────────────────────────────────────────────────────────────

export type HelloKind = "spawned" | "reconnected";

export function handleIpcHello(
  registry: Registry,
  sessionId: string,
  socketId: string,
): { id: string; label: string; kind: HelloKind } | null {
  const s = registry.get(sessionId);
  if (!s) return null;
  const wasSpawning = s.state === "spawning";
  const wasDetached = !s.socketId;  // check before attach
  registry.attachSocket(sessionId, socketId);
  registry.updateState(sessionId, { state: "idle" });
  if (wasSpawning) return { id: s.id, label: s.label, kind: "spawned" };
  if (wasDetached) return { id: s.id, label: s.label, kind: "reconnected" };
  return null;
}

export function handleIpcSignal(
  registry: Registry,
  socketId: string,
  signal: string,
): void {
  const session = registry.getBySocketId(socketId);
  if (!session) return;
  registry.updateState(session.id, { signal: signal as never });
}

export function handleIpcPermissionRequest(
  registry: Registry,
  permissionToSession: Map<string, string>,
  pendingPermissions: Map<string, PermissionInfo>,
  tg: PermissionTelegramDeps,
  allowlist: string[],
  buildKeyboard: BuildKeyboardFn,
  formatPrompt: FormatPromptFn,
  msg: Extract<IpcMessage, { op: "permission_request" }>,
): void {
  permissionToSession.set(msg.request_id, msg.session_id);
  pendingPermissions.set(msg.request_id, {
    tool_name: msg.tool_name,
    description: msg.description,
    input_preview: msg.input_preview,
  });
  const session = registry.get(msg.session_id);
  const label = session ? `[${session.id}][${session.label}] ` : "";
  const keyboard = buildKeyboard(msg.request_id);
  const prompt = `${label}${formatPrompt(msg.tool_name)}`;
  for (const chatId of allowlist) {
    void tg.sendWithKeyboard(chatId, prompt, keyboard).catch((err) => {
      anomaly.log("channel_reply_failed", {
        op: "permission_request_relay",
        chatId,
        requestId: msg.request_id,
        error: String(err),
      });
    });
  }
}

// ── Inbound message handler ───────────────────────────────────────────────────

/**
 * active 세션이 없을 때의 안내 메시지 — registry 상태별로 분기.
 *  - 살아있는 세션 0개:           /new · /resume 안내
 *  - 모두 SSH(noAutoSwitch) 세션: F6 handoff 안내 (SSH 세션은 자동 active 전환 안 됨)
 *  - 그 외:                      일반 안내
 */
function buildNoActiveAnnounce(registry: Registry, hasSocketIssue: boolean): string {
  if (hasSocketIssue) {
    return "⚠️ active 세션 소켓 없음 — 세션이 끊긴 상태입니다. /sessions 으로 확인";
  }
  const alive = registry.list().filter((s) => s.state !== "dead");
  if (alive.length === 0) {
    return "⚠️ active 세션 없음 — /new 또는 /resume 으로 시작하세요";
  }
  if (alive.every((s) => s.noAutoSwitch)) {
    const list = alive.map((s) => `[${s.id}]${s.label}`).join(", ");
    return (
      `⚠️ active 세션 없음 — SSH 에서 생성한 세션(${list})은 자동 전환되지 않습니다.\n` +
      `해당 SSH 세션에서 F6 을 눌러 handoff 하거나, /new 로 새 세션을 생성하세요.`
    );
  }
  return "⚠️ active 세션 없음 — /sessions 으로 상태 확인";
}

export function handleInbound(
  registry: Registry,
  sockets: Map<string, LineSocket>,
  announce: (text: string) => void,
  evt: InboundEvent,
): boolean {
  const active = registry.active();
  if (!active || !active.socketId) {
    announce(buildNoActiveAnnounce(registry, !!active));
    anomaly.log("inbound_no_active_session", {
      reason: active ? "socket_missing" : "no_session",
      preview: evt.content.slice(0, 40),
    });
    return false;
  }
  const ls = sockets.get(active.socketId);
  if (!ls) {
    anomaly.log("inbound_no_active_session", {
      reason: "socket_map_miss",
      sessionId: active.id,
    });
    return false;
  }
  // ChannelInboundMeta 는 string | undefined 를 허용하므로 IpcInbound 의
  // Record<string, string> 으로 보내기 전 undefined 키를 제거한다.
  const cleanMeta: Record<string, string> = {};
  for (const [k, v] of Object.entries(evt.meta)) {
    if (v !== undefined) cleanMeta[k] = v;
  }
  ls.send({
    op: "inbound",
    content: evt.content,
    meta: cleanMeta,
  });
  registry.updateState(active.id, { inboundAt: Date.now() });
  return true;
}

// ── Permission reply ──────────────────────────────────────────────────────────

export function deliverPermissionReply(
  permissionToSession: Map<string, string>,
  pendingPermissions: Map<string, PermissionInfo>,
  sockets: Map<string, LineSocket>,
  registry: Registry,
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
