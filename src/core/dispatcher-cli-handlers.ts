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
  // active 변경(set/clear) 시 dispatcher 가 채널 fan-out + IPC push 처리.
  // clear_active 핸들러에서만 사용 — 다른 명령은 통과.
  announce?: (text: string) => void;
  onActiveChangedRequest?: (previousId: string | undefined) => void;
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
      case "clear_active":
        return handleClearActive(msg, deps);
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
    noAutoSwitch: !!s.noAutoSwitch,
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
  const opts: { cwd?: string; skipPermissions?: boolean; autoSwitch?: boolean } = { autoSwitch: false };
  if (msg.cwd) opts.cwd = msg.cwd;
  // explicit false 도 흡수해야 cb-menu PermissionPrompt 의 "ask each time" 선택이
  // config.skipPermissions=true 환경에서 무시되지 않는다 (이전 truthy 체크는 false 를 떨궈냈음).
  if (msg.skipPermissions !== undefined) opts.skipPermissions = msg.skipPermissions;
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
    false,  // CLI(SSH) 트리거 — Telegram active 자동 전환 억제
  );
  return ok(msg.request_id, { kind: "resume", message });
}

function handleClearActive(
  msg: Extract<CliRequest, { command: "clear_active" }>,
  deps: CliHandlerDeps,
): CliResponse {
  const prev = deps.registry.active()?.id;
  if (prev !== msg.expected_session_id) {
    // race: 이미 다른 세션이 active 로 바뀜 — 무시
    return ok(msg.request_id, {
      kind: "clear_active",
      cleared: false,
      reason: prev ? `current_active=${prev}` : "no_active",
    });
  }
  const target = deps.registry.get(prev);
  deps.registry.clearActive();
  // noAutoSwitch=true 로 설정해 라벨이 [SSH] 로 전환되도록.
  // noAutoSwitch 는 더 이상 "spawn origin" 이 아니라 "현재 ownership" 마커.
  deps.registry.updateState(prev, { noAutoSwitch: true });
  if (target) {
    deps.announce?.(
      `🔀 handoff: [${target.id}][${target.label}] → SSH active`,
    );
  }
  deps.onActiveChangedRequest?.(prev);
  return ok(msg.request_id, { kind: "clear_active", cleared: true });
}
