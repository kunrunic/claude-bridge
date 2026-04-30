/**
 * dispatcher CLI 핸들러 — cb 명령(`cb sessions`, `cb new`, ...) 에서 들어오는
 * cli_request 를 처리하고 cli_response 로 응답.
 *
 * MCP server 의 IpcInbound / IpcPermissionRequest 와 다른 점:
 *  - request/response 1:1 — 클라이언트가 응답 받고 즉시 connection close
 *  - 채널 무관 — Telegram 처리와 별개
 *
 * 비동기 spawn 의 경우 hello IPC 도착 전에 응답 반환 (id, label 만 결정됨).
 * 사용자가 그 후 `cb attach <id>` 로 tmux 접속.
 */

import type { Registry } from "./registry.ts";
import type { SessionManager } from "./SessionManager.ts";
import type { CliRequest, CliResponse, CliSessionInfo, CliRecentInfo } from "./ipc.ts";

export type CliHandlerDeps = {
  registry: Registry;
  sessions: SessionManager;
};

function ok(requestId: string, data: NonNullable<CliResponse["data"]>): CliResponse {
  return { op: "cli_response", request_id: requestId, ok: true, data };
}

function err(requestId: string, message: string): CliResponse {
  return { op: "cli_response", request_id: requestId, ok: false, error: message };
}

export function handleCliRequest(
  msg: CliRequest,
  deps: CliHandlerDeps,
): CliResponse {
  try {
    switch (msg.command) {
      case "list_sessions":
        return handleListSessions(msg.request_id, deps);
      case "spawn":
        return handleSpawn(msg, deps);
      case "kill":
        return handleKill(msg, deps);
      case "list_recent":
        return handleListRecent(msg, deps);
      case "resume":
        return handleResume(msg, deps);
    }
  } catch (e) {
    return err(msg.request_id, String(e));
  }
}

function handleListSessions(requestId: string, deps: CliHandlerDeps): CliResponse {
  const active = deps.registry.active();
  const sessions: CliSessionInfo[] = deps.registry.list().map((s) => ({
    id: s.id,
    label: s.label,
    tmuxName: s.tmuxName,
    state: s.state,
    signal: s.signal,
    isActive: active?.id === s.id,
  }));
  const data: { kind: "list_sessions"; sessions: CliSessionInfo[]; activeId?: string } = {
    kind: "list_sessions",
    sessions,
  };
  if (active) data.activeId = active.id;
  return ok(requestId, data);
}

function handleSpawn(
  msg: Extract<CliRequest, { command: "spawn" }>,
  deps: CliHandlerDeps,
): CliResponse {
  const opts: { cwd?: string; skipPermissions?: boolean } = {};
  if (msg.cwd) opts.cwd = msg.cwd;
  if (msg.skipPermissions) opts.skipPermissions = msg.skipPermissions;
  const r = deps.sessions.spawn(opts);
  // tmuxName 은 registry 에 spawn 직후 setting 됨. 클라이언트가 즉시 attach 할 수 있도록 응답에 포함.
  const session = deps.registry.get(r.id);
  const tmuxName = session?.tmuxName ?? "";
  return ok(msg.request_id, { kind: "spawn", id: r.id, label: r.label, tmuxName });
}

function handleKill(
  msg: Extract<CliRequest, { command: "kill" }>,
  deps: CliHandlerDeps,
): CliResponse {
  const success = deps.sessions.kill(msg.target);
  const data: { kind: "kill"; success: boolean; message?: string } = {
    kind: "kill",
    success,
  };
  if (!success) data.message = `no such session: ${msg.target}`;
  return ok(msg.request_id, data);
}

function handleListRecent(
  msg: Extract<CliRequest, { command: "list_recent" }>,
  deps: CliHandlerDeps,
): CliResponse {
  // 호출자가 limit 지정 안 하면 기본 8 — telegram /resume 처럼 메시지 길이 부담이
  // 있는 매체용. cb-menu 같은 TUI 는 화면이 넓어 더 큰 값을 명시적으로 요청.
  const recent: CliRecentInfo[] = deps.sessions
    .refreshPickerCache(msg.limit)
    .map((s) => ({
      id: s.id,
      project: s.project,
      title: s.title,
      mtimeText: s.mtimeText,
    }));
  return ok(msg.request_id, { kind: "list_recent", recent });
}

function handleResume(
  msg: Extract<CliRequest, { command: "resume" }>,
  deps: CliHandlerDeps,
): CliResponse {
  const message = deps.sessions.resume(
    msg.target,
    msg.fork ?? false,
    msg.skipPermissions ?? false,
  );
  return ok(msg.request_id, { kind: "resume", message });
}
