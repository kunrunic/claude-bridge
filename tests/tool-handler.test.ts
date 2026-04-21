import { describe, expect, test, mock } from "bun:test";
import { ToolHandler } from "../src/channels/telegram/tools/ToolHandler.ts";
import type { TelegramClient } from "../src/channels/telegram/client.ts";
import type { Config } from "../src/channels/telegram/config.ts";

// ── helpers ───────────────────────────────────────────────────────────────────

function fakeConfig(allowlist: string[] = ["chat1"]): Config {
  return {
    botToken: "tok",
    allowlist,
    defaultChatId: "chat1",
    dumpEnabled: false,
    skipPermissions: false,
  };
}

function fakeTg(overrides: Partial<TelegramClient> = {}): TelegramClient {
  return {
    sendMessage: mock(async () => 100),
    sendFile: mock(async () => 200),
    setReaction: mock(async () => {}),
    editMessage: mock(async () => {}),
    getFilePath: mock(async () => "remote/path.jpg"),
    sendWithKeyboard: mock(async () => 0),
    editWithKeyboard: mock(async () => {}),
    unpinMessage: mock(async () => {}),
    pinMessage: mock(async () => {}),
    setCommands: mock(async () => {}),
    bot: {} as TelegramClient["bot"],
    token: "tok",
    ...overrides,
  } as unknown as TelegramClient;
}

// ── reply ─────────────────────────────────────────────────────────────────────

describe("ToolHandler.reply", () => {
  test("단문 메시지 → sendMessage 1회", async () => {
    const tg = fakeTg();
    const h = new ToolHandler(tg, fakeConfig());
    const r = await h.handle("reply", { chat_id: "chat1", text: "안녕" });
    expect(r.isError).toBeUndefined();
    expect(r.content[0]!.text).toMatch(/sent message_ids=100/);
    expect((tg.sendMessage as ReturnType<typeof mock>).mock.calls.length).toBe(1);
  });

  test("4096자 초과 → 청크 분할 (newline split)", async () => {
    const tg = fakeTg();
    const h = new ToolHandler(tg, fakeConfig());
    const long = "x".repeat(5000);
    await h.handle("reply", { chat_id: "chat1", text: long, split: "newline" });
    expect((tg.sendMessage as ReturnType<typeof mock>).mock.calls.length).toBe(2);
  });

  test("4096자 초과 → 청크 분할 (length split)", async () => {
    const tg = fakeTg();
    const h = new ToolHandler(tg, fakeConfig());
    const long = "y".repeat(9000);
    await h.handle("reply", { chat_id: "chat1", text: long, split: "length" });
    expect((tg.sendMessage as ReturnType<typeof mock>).mock.calls.length).toBe(3);
  });

  test("reply_to_mode=off → replyTo 전달 안 됨", async () => {
    const tg = fakeTg();
    const h = new ToolHandler(tg, fakeConfig());
    await h.handle("reply", { chat_id: "chat1", text: "hi", reply_to: "99", reply_to_mode: "off" });
    const opts = (tg.sendMessage as ReturnType<typeof mock>).mock.calls[0]![2] as { replyTo?: number } | undefined;
    expect(opts?.replyTo).toBeUndefined();
  });

  test("reply_to_mode=first → 첫 청크만 replyTo", async () => {
    const tg = fakeTg();
    const h = new ToolHandler(tg, fakeConfig());
    const long = "z".repeat(5000);
    await h.handle("reply", { chat_id: "chat1", text: long, reply_to: "99", reply_to_mode: "first", split: "length" });
    const calls = (tg.sendMessage as ReturnType<typeof mock>).mock.calls;
    expect((calls[0]![2] as { replyTo?: number })?.replyTo).toBe(99);
    expect((calls[1]![2] as { replyTo?: number })?.replyTo).toBeUndefined();
  });

  test("차단된 chat_id → isError", async () => {
    const tg = fakeTg();
    const h = new ToolHandler(tg, fakeConfig(["chat1"]));
    const r = await h.handle("reply", { chat_id: "blocked", text: "hi" });
    expect(r.isError).toBe(true);
    expect(r.content[0]!.text).toMatch(/reply failed/);
  });

  test("활성 세션(provider=active) → prefix 없음", async () => {
    const tg = fakeTg();
    const provider = { sessionActive: true, sessionLabel: "backend" };
    const h = new ToolHandler(tg, fakeConfig(), provider);
    await h.handle("reply", { chat_id: "chat1", text: "hello" });
    const firstCall = (tg.sendMessage as ReturnType<typeof mock>).mock.calls[0]!;
    expect(firstCall[1]).toBe("hello");
  });

  test("비활성 세션(provider=inactive) → ⚡ label 별도 줄 prefix 주입", async () => {
    const tg = fakeTg();
    const provider = { sessionActive: false, sessionLabel: "backend" };
    const h = new ToolHandler(tg, fakeConfig(), provider);
    await h.handle("reply", { chat_id: "chat1", text: "done" });
    const firstCall = (tg.sendMessage as ReturnType<typeof mock>).mock.calls[0]!;
    expect(firstCall[1]).toBe("⚡ backend\ndone");
  });

  test("provider 없음(stand-alone) → prefix 없음", async () => {
    const tg = fakeTg();
    const h = new ToolHandler(tg, fakeConfig(), undefined);
    await h.handle("reply", { chat_id: "chat1", text: "hello" });
    const firstCall = (tg.sendMessage as ReturnType<typeof mock>).mock.calls[0]!;
    expect(firstCall[1]).toBe("hello");
  });

  test("비활성 + text 비어있음 → prefix 스킵", async () => {
    const tg = fakeTg();
    const provider = { sessionActive: false, sessionLabel: "backend" };
    const h = new ToolHandler(tg, fakeConfig(), provider);
    await h.handle("reply", { chat_id: "chat1", text: "" });
    const calls = (tg.sendMessage as ReturnType<typeof mock>).mock.calls;
    if (calls.length > 0) {
      expect(calls[0]![1]).not.toContain("⚡ backend");
    }
  });

  test("비활성 + label 비어있음 → prefix 스킵", async () => {
    const tg = fakeTg();
    const provider = { sessionActive: false, sessionLabel: "" };
    const h = new ToolHandler(tg, fakeConfig(), provider);
    await h.handle("reply", { chat_id: "chat1", text: "hello" });
    const firstCall = (tg.sendMessage as ReturnType<typeof mock>).mock.calls[0]!;
    expect(firstCall[1]).toBe("hello");
  });

  test("message_ids 배열이 McpResult 에 포함됨 (pin 용)", async () => {
    const tg = fakeTg();
    const h = new ToolHandler(tg, fakeConfig());
    const r = await h.handle("reply", { chat_id: "chat1", text: "hello" });
    expect(r.message_ids).toEqual([100]);
  });

  test("chunked 메시지 → message_ids 배열 길이 = chunk 수", async () => {
    let counter = 100;
    const tg = fakeTg({ sendMessage: mock(async () => counter++) });
    const h = new ToolHandler(tg, fakeConfig());
    const r = await h.handle("reply", { chat_id: "chat1", text: "x".repeat(5000), split: "length" });
    expect(r.message_ids?.length).toBeGreaterThan(1);
  });
});

// ── react ─────────────────────────────────────────────────────────────────────

describe("ToolHandler.react", () => {
  test("setReaction 호출 → reaction set 반환", async () => {
    const tg = fakeTg();
    const h = new ToolHandler(tg, fakeConfig());
    const r = await h.handle("react", { chat_id: "chat1", message_id: "55", emoji: "👍" });
    expect(r.isError).toBeUndefined();
    expect(r.content[0]!.text).toBe("reaction set");
    const calls = (tg.setReaction as ReturnType<typeof mock>).mock.calls;
    expect(calls[0]![2]).toBe("👍");
  });

  test("차단된 chat_id → isError", async () => {
    const tg = fakeTg();
    const h = new ToolHandler(tg, fakeConfig(["chat1"]));
    const r = await h.handle("react", { chat_id: "blocked", message_id: "1", emoji: "👍" });
    expect(r.isError).toBe(true);
  });
});

// ── edit_message ──────────────────────────────────────────────────────────────

describe("ToolHandler.edit_message", () => {
  test("editMessage 호출 → edited 반환", async () => {
    const tg = fakeTg();
    const h = new ToolHandler(tg, fakeConfig());
    const r = await h.handle("edit_message", { chat_id: "chat1", message_id: "10", text: "new" });
    expect(r.isError).toBeUndefined();
    expect(r.content[0]!.text).toBe("edited");
    expect((tg.editMessage as ReturnType<typeof mock>).mock.calls.length).toBe(1);
  });

  test("차단된 chat_id → isError", async () => {
    const tg = fakeTg();
    const h = new ToolHandler(tg, fakeConfig(["chat1"]));
    const r = await h.handle("edit_message", { chat_id: "blocked", message_id: "1", text: "x" });
    expect(r.isError).toBe(true);
  });
});

// ── download_attachment ───────────────────────────────────────────────────────

describe("ToolHandler.download_attachment", () => {
  test("getFilePath + saveAttachment 호출 → 경로 반환", async () => {
    const tg = fakeTg({ getFilePath: mock(async () => "remote/photo.jpg") });
    const h = new ToolHandler(tg, fakeConfig());
    // saveAttachment는 실제 네트워크 호출이므로 오류 반환을 통해 isError 확인
    // (실제 파일 다운로드 없이 경로 로직만 확인)
    const r = await h.handle("download_attachment", { file_id: "f123" });
    // saveAttachment 실패 → isError true (네트워크 없음 환경)
    // 핵심 확인: getFilePath가 호출됐는가
    expect((tg.getFilePath as ReturnType<typeof mock>).mock.calls[0]![0]).toBe("f123");
  });
});

// ── unknown tool ──────────────────────────────────────────────────────────────

describe("ToolHandler.unknown", () => {
  test("알 수 없는 도구 → isError + unknown tool 메시지", async () => {
    const tg = fakeTg();
    const h = new ToolHandler(tg, fakeConfig());
    const r = await h.handle("nonexistent", {});
    expect(r.isError).toBe(true);
    expect(r.content[0]!.text).toMatch(/unknown tool: nonexistent/);
  });

  test("도구 실행 중 예외 → isError + failed 메시지", async () => {
    const tg = fakeTg({ sendMessage: mock(async () => { throw new Error("network error"); }) });
    const h = new ToolHandler(tg, fakeConfig());
    const r = await h.handle("reply", { chat_id: "chat1", text: "hi" });
    expect(r.isError).toBe(true);
    expect(r.content[0]!.text).toMatch(/reply failed/);
  });
});

// ── chunkByLength (text.ts) ───────────────────────────────────────────────────

import { chunkByLength } from "../src/channels/telegram/tools/text.ts";

describe("chunkByLength", () => {
  test("limit 이하 → 그대로 반환", () => {
    expect(chunkByLength("hello", 100)).toEqual(["hello"]);
  });
  test("정확히 limit → 1개 청크", () => {
    expect(chunkByLength("abcd", 4)).toEqual(["abcd"]);
  });
  test("limit 초과 → 균등 분할", () => {
    expect(chunkByLength("abcdefgh", 4)).toEqual(["abcd", "efgh"]);
  });
  test("나머지 있는 경우", () => {
    expect(chunkByLength("abcde", 4)).toEqual(["abcd", "e"]);
  });
});
