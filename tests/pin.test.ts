import { describe, expect, test, mock } from "bun:test";
import { Registry } from "../src/core/registry.ts";
import { updateActivePin, type PinTelegramDeps } from "../src/core/pin.ts";

const CHAT = "chat-1";

function makeTg(sendResult = 100): {
  tg: PinTelegramDeps;
  sendCount: () => number;
  pinCount: () => number;
  unpinCount: () => number;
  lastPinned: () => number | undefined;
  lastUnpinned: () => number | undefined;
} {
  let sendCount = 0;
  let pinCount = 0;
  let unpinCount = 0;
  let lastPinned: number | undefined;
  let lastUnpinned: number | undefined;

  const tg: PinTelegramDeps = {
    sendMessage: mock(async (_chatId: string, _text: string) => {
      sendCount++;
      return sendResult;
    }),
    pinMessage: mock(async (_chatId: string, msgId: number) => {
      pinCount++;
      lastPinned = msgId;
    }),
    editMessage: mock(async () => {}),
    unpinMessage: mock(async (_chatId: string, msgId: number) => {
      unpinCount++;
      lastUnpinned = msgId;
    }),
  };

  return {
    tg,
    sendCount: () => sendCount,
    pinCount: () => pinCount,
    unpinCount: () => unpinCount,
    lastPinned: () => lastPinned,
    lastUnpinned: () => lastUnpinned,
  };
}

describe("updateActivePin", () => {
  test("no active session, no existing pin → no-op", async () => {
    const registry = new Registry();
    const { tg, sendCount, pinCount, unpinCount } = makeTg();
    await updateActivePin(registry, tg, CHAT);
    expect(sendCount()).toBe(0);
    expect(pinCount()).toBe(0);
    expect(unpinCount()).toBe(0);
  });

  test("no active session, stale pin → unpin only", async () => {
    const registry = new Registry();
    registry.setActivePin({ chatId: CHAT, messageId: 42 });
    const { tg, sendCount, pinCount, unpinCount, lastUnpinned } = makeTg();
    await updateActivePin(registry, tg, CHAT);
    expect(sendCount()).toBe(0);
    expect(pinCount()).toBe(0);
    expect(unpinCount()).toBe(1);
    expect(lastUnpinned()).toBe(42);
    expect(registry.getActivePin()).toBeUndefined();
  });

  test("active session, no existing pin → send + pin once", async () => {
    const registry = new Registry();
    registry.create("alpha");
    const { tg, sendCount, pinCount, unpinCount } = makeTg(100);
    await updateActivePin(registry, tg, CHAT);
    expect(sendCount()).toBe(1);
    expect(pinCount()).toBe(1);
    expect(unpinCount()).toBe(0);
    expect(registry.getActivePin()).toEqual({ chatId: CHAT, messageId: 100 });
  });

  test("active 바뀜 + 기존 pin 존재 → editMessage 로 재사용 (같은 messageId 유지)", async () => {
    const registry = new Registry();
    registry.create("alpha");
    registry.setActivePin({ chatId: CHAT, messageId: 55 });
    const { tg, sendCount, pinCount, unpinCount } = makeTg(99);
    await updateActivePin(registry, tg, CHAT);
    // edit 재사용이라 send/pin/unpin 호출 없음
    expect(sendCount()).toBe(0);
    expect(pinCount()).toBe(0);
    expect(unpinCount()).toBe(0);
    expect(tg.editMessage).toHaveBeenCalledWith(CHAT, 55, expect.stringContaining("alpha"));
    expect(registry.getActivePin()).toEqual({ chatId: CHAT, messageId: 55 });
  });

  test("연속 호출 → 기존 pin 을 edit 으로 재사용 (새 send/pin 없음)", async () => {
    // 첫 호출은 send + pin, 이후 호출은 editMessage 로 재사용. 채팅에 pin 메시지가
    // 반복해서 쌓이지 않도록 하기 위함.
    const registry = new Registry();
    registry.create("alpha");
    const { tg, sendCount, pinCount } = makeTg(100);
    await updateActivePin(registry, tg, CHAT);
    await updateActivePin(registry, tg, CHAT);
    await updateActivePin(registry, tg, CHAT);
    expect(sendCount()).toBe(1);
    expect(pinCount()).toBe(1);
    expect(tg.editMessage).toHaveBeenCalledTimes(2);
  });

  test("editMessage 가 'message is not modified' 반환 시 no-op (새 pin 생성 안 함)", async () => {
    const registry = new Registry();
    registry.create("alpha");
    registry.setActivePin({ chatId: CHAT, messageId: 55 });
    const tg: PinTelegramDeps = {
      sendMessage: mock(async () => { throw new Error("should not be called"); }),
      editMessage: mock(async () => {
        throw new Error("Bad Request: message is not modified");
      }),
      pinMessage: mock(async () => {}),
      unpinMessage: mock(async () => {}),
    };
    await updateActivePin(registry, tg, CHAT);
    expect(tg.sendMessage).not.toHaveBeenCalled();
    expect(tg.pinMessage).not.toHaveBeenCalled();
    expect(registry.getActivePin()).toEqual({ chatId: CHAT, messageId: 55 });
  });

  test("editMessage 실패 시 새 pin 메시지로 fallback", async () => {
    const registry = new Registry();
    registry.create("alpha");
    registry.setActivePin({ chatId: CHAT, messageId: 55 });
    let sent = 0;
    const tg: PinTelegramDeps = {
      sendMessage: mock(async () => { sent++; return 99; }),
      editMessage: mock(async () => { throw new Error("message not found"); }),
      pinMessage: mock(async () => {}),
      unpinMessage: mock(async () => {}),
    };
    await updateActivePin(registry, tg, CHAT);
    expect(sent).toBe(1);
    expect(tg.pinMessage).toHaveBeenCalledWith(CHAT, 99, true);
    expect(registry.getActivePin()).toEqual({ chatId: CHAT, messageId: 99 });
  });

  test("sendMessage 실패 → pin 미호출, registry 미변경", async () => {
    const registry = new Registry();
    registry.create("alpha");
    const failTg: PinTelegramDeps = {
      sendMessage: mock(async () => { throw new Error("network"); }),
      pinMessage: mock(async () => {}),
      unpinMessage: mock(async () => {}),
      editMessage: mock(async () => {}),
    };
    await updateActivePin(registry, failTg, CHAT);
    expect(failTg.pinMessage).not.toHaveBeenCalled();
    expect(registry.getActivePin()).toBeUndefined();
  });

  test("kill 후 active 없어지면 unpin", async () => {
    const registry = new Registry();
    registry.create("alpha");
    registry.setActivePin({ chatId: CHAT, messageId: 77 });
    registry.remove("s1");
    const { tg, unpinCount, lastUnpinned } = makeTg();
    await updateActivePin(registry, tg, CHAT);
    expect(unpinCount()).toBe(1);
    expect(lastUnpinned()).toBe(77);
    expect(registry.getActivePin()).toBeUndefined();
  });

  test("pendingCounts 포함 시 pin 본문에 📬 라인 추가", async () => {
    const registry = new Registry();
    registry.create("alpha");   // s1 — active
    registry.create("beta");    // s2
    registry.create("gamma");   // s3
    let capturedText = "";
    const tg: PinTelegramDeps = {
      sendMessage: mock(async (_c: string, text: string) => { capturedText = text; return 1; }),
      pinMessage: mock(async () => {}),
      unpinMessage: mock(async () => {}),
      editMessage: mock(async () => {}),
    };
    const counts = new Map([["s2", 2], ["s3", 1]]);
    await updateActivePin(registry, tg, CHAT, counts);
    expect(capturedText).toContain("⚡ active: s1 (alpha)");
    expect(capturedText).toContain("📬 s2: 2");
    expect(capturedText).toContain("📬 s3: 1");
  });

  test("pendingCount=0 인 세션은 pin 에 표시 안 함", async () => {
    const registry = new Registry();
    registry.create("alpha");
    let capturedText = "";
    const tg: PinTelegramDeps = {
      sendMessage: mock(async (_c: string, text: string) => { capturedText = text; return 1; }),
      pinMessage: mock(async () => {}),
      unpinMessage: mock(async () => {}),
      editMessage: mock(async () => {}),
    };
    const counts = new Map([["s2", 0], ["s3", 0]]);
    await updateActivePin(registry, tg, CHAT, counts);
    expect(capturedText).not.toContain("📬");
  });

  test("pendingCounts undefined → 기존 동작 유지 (active 만 표시)", async () => {
    const registry = new Registry();
    registry.create("alpha");
    let capturedText = "";
    const tg: PinTelegramDeps = {
      sendMessage: mock(async (_c: string, text: string) => { capturedText = text; return 1; }),
      pinMessage: mock(async () => {}),
      unpinMessage: mock(async () => {}),
      editMessage: mock(async () => {}),
    };
    await updateActivePin(registry, tg, CHAT);
    expect(capturedText).toBe("⚡ active: s1 (alpha)");
  });

  test("시나리오: 기존 active pin 있는 상태에서 신규 세션 → edit 으로 재사용, 새 메시지 안 보냄", async () => {
    const registry = new Registry();
    // 기존 s1 active pin 상태
    const s1 = registry.create("alpha");
    registry.setActivePin({ chatId: CHAT, messageId: 1000 });

    let sentCount = 0;
    const tg: PinTelegramDeps = {
      sendMessage: mock(async () => { sentCount++; return 2000; }),
      editMessage: mock(async () => {}),
      pinMessage: mock(async () => {}),
      unpinMessage: mock(async () => {}),
    };

    // 신규 세션 스폰 후 active 바뀜
    const s2 = registry.create("beta");
    registry.setActive(s2.id);

    await updateActivePin(registry, tg, CHAT);

    expect(sentCount).toBe(0);
    expect(tg.pinMessage).not.toHaveBeenCalled();
    expect(tg.editMessage).toHaveBeenCalledWith(CHAT, 1000, expect.stringContaining("beta"));
    expect(registry.getActivePin()?.messageId).toBe(1000);
  });

  test("시나리오: 신규→신규→resume 연속 전환 시 pin 1개 유지 (editMessage 재사용)", async () => {
    const registry = new Registry();
    let nextMsgId = 100;
    const editCalls: Array<[string, number, string]> = [];
    const tg: PinTelegramDeps = {
      sendMessage: mock(async () => nextMsgId++),
      editMessage: mock(async (c: string, m: number, t: string) => {
        editCalls.push([c, m, t]);
      }),
      pinMessage: mock(async () => {}),
      unpinMessage: mock(async () => {}),
    };

    // 1. 신규 s1 생성 → 첫 pin 메시지
    const s1 = registry.create("alpha");
    registry.setActive(s1.id);
    await updateActivePin(registry, tg, CHAT);
    const firstPinId = registry.getActivePin()!.messageId;
    expect((tg.sendMessage as ReturnType<typeof mock>).mock.calls.length).toBe(1);

    // 2. 신규 s2 생성 후 전환 → editMessage 호출 (새 send 없음)
    const s2 = registry.create("beta");
    registry.setActive(s2.id);
    await updateActivePin(registry, tg, CHAT);
    expect((tg.sendMessage as ReturnType<typeof mock>).mock.calls.length).toBe(1);
    expect(editCalls[0]?.[1]).toBe(firstPinId);
    expect(editCalls[0]?.[2]).toContain("beta");

    // 3. s3 resume 후 전환 → 또 editMessage
    const s3 = registry.create("gamma");
    registry.setActive(s3.id);
    await updateActivePin(registry, tg, CHAT);
    expect((tg.sendMessage as ReturnType<typeof mock>).mock.calls.length).toBe(1);
    expect(editCalls.length).toBe(2);
    expect(editCalls[1]?.[2]).toContain("gamma");

    // pin messageId 는 시종일관 firstPinId 유지
    expect(registry.getActivePin()?.messageId).toBe(firstPinId);
  });

  test("시나리오: pendingCount 변화 시에도 pin 1개 유지 (editMessage 로 카운터 업데이트)", async () => {
    const registry = new Registry();
    const s1 = registry.create("alpha");
    const s2 = registry.create("beta");
    registry.setActive(s1.id);

    let nextMsgId = 100;
    let lastEditText = "";
    const tg: PinTelegramDeps = {
      sendMessage: mock(async () => nextMsgId++),
      editMessage: mock(async (_c: string, _m: number, t: string) => { lastEditText = t; }),
      pinMessage: mock(async () => {}),
      unpinMessage: mock(async () => {}),
    };

    // 초기 pin
    await updateActivePin(registry, tg, CHAT);
    const pinId = registry.getActivePin()!.messageId;

    // s2 에 pending count 증가
    const counts = new Map<string, number>([["s2", 1]]);
    await updateActivePin(registry, tg, CHAT, counts);
    expect(lastEditText).toContain("📬 s2: 1");

    counts.set("s2", 3);
    await updateActivePin(registry, tg, CHAT, counts);
    expect(lastEditText).toContain("📬 s2: 3");

    // send 는 최초 1회만 호출됨 (send 이후엔 edit 으로 반복 갱신)
    expect((tg.sendMessage as ReturnType<typeof mock>).mock.calls.length).toBe(1);
    expect(registry.getActivePin()?.messageId).toBe(pinId);
  });

  test("active 없이 pending 만 있으면 '⚡ no active session' + 📬 라인", async () => {
    const registry = new Registry();
    registry.create("alpha");
    registry.remove("s1");
    // 세션은 사라졌지만 카운터가 남아있을 가능성 커버 — 실제 dispatcher 는 cleanup 하지만 방어적
    let capturedText = "";
    const tg: PinTelegramDeps = {
      sendMessage: mock(async (_c: string, text: string) => { capturedText = text; return 1; }),
      pinMessage: mock(async () => {}),
      unpinMessage: mock(async () => {}),
      editMessage: mock(async () => {}),
    };
    const counts = new Map([["s1", 2]]);
    await updateActivePin(registry, tg, CHAT, counts);
    expect(capturedText).toContain("⚡ no active session");
    expect(capturedText).toContain("📬 s1: 2");
  });
});
