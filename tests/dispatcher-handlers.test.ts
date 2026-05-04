import { describe, expect, test, mock } from "bun:test";
import { Registry } from "../src/core/registry.ts";
import {
  handleIpcHello,
  handleIpcSignal,
  handleIpcPermissionRequest,
  handleInbound,
  deliverPermissionReply,
  scheduleSpawnTimeout,
  type PermissionInfo,
  type PermissionTelegramDeps,
} from "../src/core/dispatcher-handlers.ts";
import type { LineSocket } from "../src/core/ipc.ts";

// ── helpers ───────────────────────────────────────────────────────────────────

function fakeSocket(sent: unknown[] = []): LineSocket {
  return {
    send: mock((msg: unknown) => { sent.push(msg); }),
    onMessage: mock(() => {}),
    onClose: mock(() => {}),
    close: mock(() => {}),
  } as unknown as LineSocket;
}

function makeInboundEvt(content: string) {
  return {
    content,
    meta: { chat_id: "chat1", user: "user1", user_id: "111", ts: new Date().toISOString() },
  };
}

// ── IPC: hello ────────────────────────────────────────────────────────────────

describe("handleIpcHello", () => {
  test("hello → socket attached, state becomes idle", () => {
    const r = new Registry();
    r.create("alpha");
    handleIpcHello(r, "s1", "sock-A");
    const s = r.get("s1")!;
    expect(s.socketId).toBe("sock-A");
    expect(s.state).toBe("idle");
  });

  test("hello for unknown session → no crash, returns null", () => {
    const r = new Registry();
    expect(handleIpcHello(r, "s99", "sock-X")).toBeNull();
  });

  test("hello 전 state=spawning, hello 후 state=idle 전환 + label 반환", () => {
    const r = new Registry();
    r.create("alpha");
    expect(r.get("s1")!.state).toBe("spawning");
    const result = handleIpcHello(r, "s1", "sock-A");
    expect(r.get("s1")!.state).toBe("idle");
    expect(result).not.toBeNull();
    expect(result!.label).toBe("alpha");
  });

  test("hello (non-spawning session) → null 반환 (ready 알림 없음)", () => {
    const r = new Registry();
    r.create("alpha");
    handleIpcHello(r, "s1", "sock-A");
    r.updateState("s1", { state: "idle" });
    const result = handleIpcHello(r, "s1", "sock-B");
    expect(result).toBeNull();
  });
});

// ── IPC: signal ───────────────────────────────────────────────────────────────

describe("handleIpcSignal", () => {
  test("signal updates session state by socketId", () => {
    const r = new Registry();
    r.create("alpha");
    handleIpcHello(r, "s1", "sock-A");
    handleIpcSignal(r, "sock-A", "busy");
    expect(r.get("s1")!.signal).toBe("busy");
  });

  test("unknown socket → no crash", () => {
    const r = new Registry();
    expect(() => handleIpcSignal(r, "sock-ghost", "busy")).not.toThrow();
  });
});

// ── IPC: permission_request ───────────────────────────────────────────────────

describe("handleIpcPermissionRequest", () => {
  test("permission_request → keyboard sent to each allowlist chatId", async () => {
    const r = new Registry();
    r.create("alpha");
    const permToSession = new Map<string, string>();
    const pending = new Map<string, PermissionInfo>();
    const sent: Array<{ chatId: string; text: string }> = [];
    const tg: PermissionTelegramDeps = {
      sendWithKeyboard: mock(async (chatId: string, text: string) => {
        sent.push({ chatId, text });
        return 1;
      }),
    };
    handleIpcPermissionRequest(
      r, permToSession, pending, tg,
      ["chat1", "chat2"],
      (reqId) => ({ reqId }),
      (tool) => `allow ${tool}?`,
      { op: "permission_request", request_id: "req-1", session_id: "s1", tool_name: "Bash", description: "run cmd", input_preview: "ls" },
    );
    // 맵 등록 확인
    expect(permToSession.get("req-1")).toBe("s1");
    expect(pending.get("req-1")?.tool_name).toBe("Bash");
    // 2개 chatId 에 각각 전송
    await new Promise((r) => setTimeout(r, 10));
    expect(sent.length).toBe(2);
    expect(sent[0]!.chatId).toBe("chat1");
    expect(sent[1]!.chatId).toBe("chat2");
  });

  test("session label이 프롬프트에 포함", async () => {
    const r = new Registry();
    r.create("backend");
    const permToSession = new Map<string, string>();
    const pending = new Map<string, PermissionInfo>();
    const prompts: string[] = [];
    const tg: PermissionTelegramDeps = {
      sendWithKeyboard: mock(async (_chatId: string, text: string) => {
        prompts.push(text);
        return 1;
      }),
    };
    handleIpcPermissionRequest(
      r, permToSession, pending, tg, ["chat1"],
      (_) => ({}), (tool) => `allow ${tool}?`,
      { op: "permission_request", request_id: "req-2", session_id: "s1", tool_name: "Write", description: "d", input_preview: "p" },
    );
    await new Promise((r) => setTimeout(r, 10));
    expect(prompts[0]).toContain("[backend]");
  });
});

// ── Inbound message routing ───────────────────────────────────────────────────

describe("handleInbound", () => {
  test("세션 없음 → announce 호출, false 반환", () => {
    const r = new Registry();
    const sockets = new Map<string, LineSocket>();
    const announced: string[] = [];
    const result = handleInbound(r, sockets, (t) => announced.push(t), makeInboundEvt("hello"));
    expect(result).toBe(false);
    expect(announced[0]).toMatch(/active 세션 없음/);
    expect(announced[0]).toMatch(/\/new/);
  });

  test("세션 있지만 소켓 없음(spawning 상태) → announce 호출, false 반환", () => {
    const r = new Registry();
    r.create("alpha"); // state=spawning, socketId=undefined → create 가 active 로 승격
    const sockets = new Map<string, LineSocket>();
    const announced: string[] = [];
    const result = handleInbound(r, sockets, (t) => announced.push(t), makeInboundEvt("hello"));
    expect(result).toBe(false);
    expect(announced[0]).toMatch(/소켓 없음/);
  });

  test("SSH-only 세션만 존재 (active 없음) → F6 handoff 안내", () => {
    const r = new Registry();
    const s = r.create("alpha");
    r.updateState(s.id, { noAutoSwitch: true });
    // create 가 첫 세션을 auto-active 로 승격하므로 시나리오 재현 위해 명시적으로 해제.
    // 실 환경에선 active 세션이 죽거나 reconcile 로 정리된 후 SSH 세션만 남는 경우.
    r.clearActive();
    const sockets = new Map<string, LineSocket>();
    const announced: string[] = [];
    const result = handleInbound(r, sockets, (t) => announced.push(t), makeInboundEvt("hello"));
    expect(result).toBe(false);
    expect(announced[0]).toMatch(/SSH/);
    expect(announced[0]).toMatch(/F6/);
    expect(announced[0]).toMatch(/handoff/);
  });

  test("세션 + 소켓 있음 → 메시지 소켓으로 포워딩, true 반환", () => {
    const r = new Registry();
    r.create("alpha");
    handleIpcHello(r, "s1", "sock-A");
    const sent: unknown[] = [];
    const sockets = new Map<string, LineSocket>([["sock-A", fakeSocket(sent)]]);
    const result = handleInbound(r, sockets, () => {}, makeInboundEvt("안녕하세요"));
    expect(result).toBe(true);
    expect(sent.length).toBe(1);
    expect((sent[0] as { content: string }).content).toBe("안녕하세요");
  });


  test("소켓 맵에 없는 경우 → announce 없이 false 반환", () => {
    const r = new Registry();
    r.create("alpha");
    handleIpcHello(r, "s1", "sock-A");
    // sock-A를 맵에 등록하지 않음
    const sockets = new Map<string, LineSocket>();
    const announced: string[] = [];
    const result = handleInbound(r, sockets, (t) => announced.push(t), makeInboundEvt("msg"));
    expect(result).toBe(false);
    expect(announced.length).toBe(0); // 에러 알림 없음 (anomaly 로그만)
  });
});

// ── Permission reply ──────────────────────────────────────────────────────────

describe("deliverPermissionReply", () => {
  test("allow → permission_reply 소켓으로 전송", () => {
    const r = new Registry();
    r.create("alpha");
    handleIpcHello(r, "s1", "sock-A");
    const sent: unknown[] = [];
    const sockets = new Map<string, LineSocket>([["sock-A", fakeSocket(sent)]]);
    const permToSession = new Map([["req-1", "s1"]]);
    const pending = new Map<string, PermissionInfo>();
    deliverPermissionReply(permToSession, pending, sockets, r, "req-1", "allow");
    expect((sent[0] as { op: string; behavior: string }).op).toBe("permission_reply");
    expect((sent[0] as { behavior: string }).behavior).toBe("allow");
    expect(permToSession.has("req-1")).toBe(false); // 정리됨
  });

  test("deny → permission_reply deny 전송", () => {
    const r = new Registry();
    r.create("alpha");
    handleIpcHello(r, "s1", "sock-A");
    const sent: unknown[] = [];
    const sockets = new Map<string, LineSocket>([["sock-A", fakeSocket(sent)]]);
    const permToSession = new Map([["req-1", "s1"]]);
    deliverPermissionReply(permToSession, new Map(), sockets, r, "req-1", "deny");
    expect((sent[0] as { behavior: string }).behavior).toBe("deny");
  });

  test("unknown requestId → 조용히 무시", () => {
    const r = new Registry();
    const sockets = new Map<string, LineSocket>();
    expect(() =>
      deliverPermissionReply(new Map(), new Map(), sockets, r, "ghost-req", "allow")
    ).not.toThrow();
  });

  test("세션이 소켓 없음(dead) → 전송 없음", () => {
    const r = new Registry();
    r.create("alpha"); // socketId=undefined
    const sent: unknown[] = [];
    const sockets = new Map<string, LineSocket>();
    const permToSession = new Map([["req-1", "s1"]]);
    deliverPermissionReply(permToSession, new Map(), sockets, r, "req-1", "allow");
    expect(sent.length).toBe(0);
  });
});

// ── Spawn timeout ─────────────────────────────────────────────────────────────

describe("scheduleSpawnTimeout", () => {
  test("hello 미수신 → error 전환 + announce 호출", async () => {
    const r = new Registry();
    r.create("alpha");
    const announced: string[] = [];
    scheduleSpawnTimeout(r, "s1", "alpha", 20, (t) => announced.push(t));
    await new Promise((res) => setTimeout(res, 50));
    expect(r.get("s1")!.state).toBe("error");
    expect(announced[0]).toMatch(/기동 실패/);
    expect(announced[0]).toMatch(/\[alpha\]/);
  });

  test("hello 수신 후에는 타임아웃 무효 (state 유지)", async () => {
    const r = new Registry();
    r.create("alpha");
    handleIpcHello(r, "s1", "sock-A"); // spawning → idle
    const announced: string[] = [];
    scheduleSpawnTimeout(r, "s1", "alpha", 20, (t) => announced.push(t));
    await new Promise((res) => setTimeout(res, 50));
    expect(r.get("s1")!.state).toBe("idle"); // error로 전환 안 됨
    expect(announced.length).toBe(0);
  });

  test("세션이 이미 killed 된 경우 → announce 없이 조용히 무시", async () => {
    const r = new Registry();
    r.create("alpha");
    r.remove("s1");
    const announced: string[] = [];
    scheduleSpawnTimeout(r, "s1", "alpha", 20, (t) => announced.push(t));
    await new Promise((res) => setTimeout(res, 50));
    expect(announced.length).toBe(0);
  });

  test("busy/idle 상태에서는 타임아웃 무효 (side-effect 없음)", async () => {
    const r = new Registry();
    r.create("alpha");
    r.updateState("s1", { state: "busy" });
    const announced: string[] = [];
    scheduleSpawnTimeout(r, "s1", "alpha", 20, (t) => announced.push(t));
    await new Promise((res) => setTimeout(res, 50));
    expect(r.get("s1")!.state).toBe("busy");
    expect(announced.length).toBe(0);
  });
});
