import type { Registry } from "./registry.ts";
import type { IpcMessage } from "./ipc.ts";
import type { LineSocket } from "./ipc.ts";
import type { InboundEvent } from "../channels/telegram/poller.ts";
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
  const label = session ? `[${session.label}] ` : "";
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

export function handleInbound(
  registry: Registry,
  sockets: Map<string, LineSocket>,
  announce: (text: string) => void,
  evt: InboundEvent,
): boolean {
  const active = registry.active();
  if (!active || !active.socketId) {
    announce("no active session — use /new to spawn one, or /sessions");
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
  ls.send({
    op: "inbound",
    content: evt.content,
    meta: evt.meta as Record<string, string>,
  });
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
