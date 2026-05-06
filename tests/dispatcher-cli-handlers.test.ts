import { describe, expect, test, mock } from "bun:test";
import { Registry } from "../src/core/registry.ts";
import { handleCliRequest } from "../src/core/dispatcher-cli-handlers.ts";
import type { CliRequest } from "../src/core/ipc.ts";

// SessionManager 의 실제 spawn / kill 등은 무거운 의존성 (tmux + Claude 바이너리)
// 을 거치므로, CLI 핸들러 단위 테스트에서는 deps.sessions 를 stub 으로 제공한다.
type Stub = {
  spawn: ReturnType<typeof mock>;
  kill: ReturnType<typeof mock>;
  refreshPickerCache: ReturnType<typeof mock>;
  resume: ReturnType<typeof mock>;
};

function fakeSessions(overrides: Partial<Stub> = {}): Stub {
  return {
    spawn: mock(() => ({ id: "s1", label: "alpha" })),
    kill: mock(() => true),
    refreshPickerCache: mock(() => []),
    resume: mock(() => "resuming [s2][beta]..."),
    ...overrides,
  };
}

function makeRequest<T extends Omit<CliRequest, "request_id">>(t: T): CliRequest {
  return { ...t, request_id: "req-test" } as CliRequest;
}

describe("handleCliRequest: list_sessions", () => {
  test("빈 registry → 빈 목록", () => {
    const r = new Registry();
    const sessions = fakeSessions();
    const resp = handleCliRequest(
      makeRequest({ op: "cli_request", command: "list_sessions" }),
      // SessionManager 미사용
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { registry: r, sessions: sessions as any },
    );
    expect(resp.ok).toBe(true);
    expect(resp.data?.kind).toBe("list_sessions");
    if (resp.data?.kind === "list_sessions") {
      expect(resp.data.sessions).toEqual([]);
      expect(resp.data.activeId).toBeUndefined();
    }
  });

  test("세션 2개 + active 1개 → 정확한 isActive 플래그", () => {
    const r = new Registry();
    const a = r.create("alpha");
    const b = r.create("bravo");
    r.setActive(b.id);

    const resp = handleCliRequest(
      makeRequest({ op: "cli_request", command: "list_sessions" }),
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { registry: r, sessions: fakeSessions() as any },
    );
    expect(resp.ok).toBe(true);
    if (resp.data?.kind !== "list_sessions") throw new Error("wrong kind");
    expect(resp.data.sessions.length).toBe(2);
    expect(resp.data.activeId).toBe(b.id);
    const map = Object.fromEntries(resp.data.sessions.map((s) => [s.id, s.isActive]));
    expect(map[a.id]).toBe(false);
    expect(map[b.id]).toBe(true);
  });
});

describe("handleCliRequest: spawn", () => {
  test("cwd 전달, SessionManager.spawn 호출, 결과 반환", () => {
    const sessions = fakeSessions({
      spawn: mock(() => ({ id: "s7", label: "myproj" })),
    });
    const resp = handleCliRequest(
      makeRequest({ op: "cli_request", command: "spawn", cwd: "/tmp/proj" }),
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { registry: new Registry(), sessions: sessions as any },
    );
    expect(resp.ok).toBe(true);
    if (resp.data?.kind !== "spawn") throw new Error("wrong kind");
    expect(resp.data.id).toBe("s7");
    expect(resp.data.label).toBe("myproj");
    expect(sessions.spawn.mock.calls.length).toBe(1);
    expect(sessions.spawn.mock.calls[0]![0]).toEqual({ cwd: "/tmp/proj", autoSwitch: false });
  });

  test("spawn 실패 (SessionManager throw) → ok=false + error", () => {
    const sessions = fakeSessions({
      spawn: mock(() => {
        throw new Error("tmux not found");
      }),
    });
    const resp = handleCliRequest(
      makeRequest({ op: "cli_request", command: "spawn" }),
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { registry: new Registry(), sessions: sessions as any },
    );
    expect(resp.ok).toBe(false);
    expect(resp.error).toContain("tmux not found");
  });

  test("skipPermissions=true 전달", () => {
    const sessions = fakeSessions();
    handleCliRequest(
      makeRequest({
        op: "cli_request",
        command: "spawn",
        cwd: "/x",
        skipPermissions: true,
      }),
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { registry: new Registry(), sessions: sessions as any },
    );
    expect(sessions.spawn.mock.calls[0]![0]).toEqual({
      cwd: "/x",
      skipPermissions: true,
      autoSwitch: false,
    });
  });

  test("skipPermissions=false (PermissionPrompt 'ask each time') 도 명시 전달", () => {
    // 이전 핸들러는 truthy 체크라 false 를 떨궈 config.skipPermissions 로 fallthrough 됐음.
    // cb-menu PermissionPrompt 가 'n' 으로 선택한 의도가 그대로 spawn opts 에 반영돼야 함.
    const sessions = fakeSessions();
    handleCliRequest(
      makeRequest({
        op: "cli_request",
        command: "spawn",
        cwd: "/y",
        skipPermissions: false,
      }),
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { registry: new Registry(), sessions: sessions as any },
    );
    expect(sessions.spawn.mock.calls[0]![0]).toEqual({
      cwd: "/y",
      skipPermissions: false,
      autoSwitch: false,
    });
  });
});

describe("handleCliRequest: kill", () => {
  test("성공 시 success=true", () => {
    const sessions = fakeSessions({ kill: mock(() => true) });
    const resp = handleCliRequest(
      makeRequest({ op: "cli_request", command: "kill", target: "s1" }),
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { registry: new Registry(), sessions: sessions as any },
    );
    expect(resp.ok).toBe(true);
    if (resp.data?.kind !== "kill") throw new Error("wrong kind");
    expect(resp.data.success).toBe(true);
    expect(sessions.kill.mock.calls[0]![0]).toBe("s1");
  });

  test("실패 시 success=false + message", () => {
    const sessions = fakeSessions({ kill: mock(() => false) });
    const resp = handleCliRequest(
      makeRequest({ op: "cli_request", command: "kill", target: "s99" }),
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { registry: new Registry(), sessions: sessions as any },
    );
    if (resp.data?.kind !== "kill") throw new Error("wrong kind");
    expect(resp.data.success).toBe(false);
    expect(resp.data.message).toContain("s99");
  });
});

describe("handleCliRequest: list_recent", () => {
  test("SessionManager.refreshPickerCache 결과 매핑", () => {
    const sessions = fakeSessions({
      refreshPickerCache: mock(() => [
        { id: "abc123", project: "myapp", title: "fix bug", mtimeText: "2h" },
      ]),
    });
    const resp = handleCliRequest(
      makeRequest({ op: "cli_request", command: "list_recent" }),
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { registry: new Registry(), sessions: sessions as any },
    );
    expect(resp.ok).toBe(true);
    if (resp.data?.kind !== "list_recent") throw new Error("wrong kind");
    expect(resp.data.recent).toEqual([
      { id: "abc123", project: "myapp", title: "fix bug", mtimeText: "2h" },
    ]);
  });
});

describe("handleCliRequest: resume", () => {
  test("SessionManager.resume 호출 + 결과 메시지 반환", () => {
    const sessions = fakeSessions({
      resume: mock(() => "resuming [s3][gamma]..."),
    });
    const resp = handleCliRequest(
      makeRequest({ op: "cli_request", command: "resume", target: "abc" }),
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { registry: new Registry(), sessions: sessions as any },
    );
    expect(resp.ok).toBe(true);
    if (resp.data?.kind !== "resume") throw new Error("wrong kind");
    expect(resp.data.message).toContain("resuming");
    expect(sessions.resume.mock.calls[0]).toEqual(["abc", false, false, false]);
  });

  test("fork=true, skipPermissions=true 전달", () => {
    const sessions = fakeSessions();
    handleCliRequest(
      makeRequest({
        op: "cli_request",
        command: "resume",
        target: "1",
        fork: true,
        skipPermissions: true,
      }),
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { registry: new Registry(), sessions: sessions as any },
    );
    expect(sessions.resume.mock.calls[0]).toEqual(["1", true, true, false]);
  });
});

describe("handleCliRequest: clear_active", () => {
  test("active 가 expected 와 일치 → clear + noAutoSwitch=true + announce + onActiveChangedRequest", () => {
    const r = new Registry();
    const s = r.create("alpha");
    r.setActive(s.id);
    const announced: string[] = [];
    const activeChangedCalls: Array<string | undefined> = [];
    const sessions = fakeSessions();
    const resp = handleCliRequest(
      makeRequest({
        op: "cli_request",
        command: "clear_active",
        expected_session_id: s.id,
      }),
      {
        registry: r,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        sessions: sessions as any,
        announce: (t) => announced.push(t),
        onActiveChangedRequest: (prev) => activeChangedCalls.push(prev),
      },
    );
    expect(resp.ok).toBe(true);
    if (resp.data?.kind !== "clear_active") throw new Error("wrong kind");
    expect(resp.data.cleared).toBe(true);
    expect(r.active()).toBeUndefined();
    // ownership 모델: SSH 가 control 을 가져갔으므로 noAutoSwitch=true
    expect(r.get(s.id)?.noAutoSwitch).toBe(true);
    expect(announced[0]).toMatch(/handoff.*→ SSH active/);
    expect(activeChangedCalls).toEqual([s.id]);
  });

  test("active 가 expected 와 다름 → no-op, cleared=false", () => {
    const r = new Registry();
    const a = r.create("alpha");
    const b = r.create("beta");
    r.setActive(b.id);  // active = b, not a
    const sessions = fakeSessions();
    const resp = handleCliRequest(
      makeRequest({
        op: "cli_request",
        command: "clear_active",
        expected_session_id: a.id,
      }),
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { registry: r, sessions: sessions as any },
    );
    expect(resp.ok).toBe(true);
    if (resp.data?.kind !== "clear_active") throw new Error("wrong kind");
    expect(resp.data.cleared).toBe(false);
    expect(r.active()?.id).toBe(b.id);  // 그대로 유지
  });
});
