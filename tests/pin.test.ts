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

  test("active session switch → new pin sent, old unpinned", async () => {
    const registry = new Registry();
    registry.create("alpha");
    registry.setActivePin({ chatId: CHAT, messageId: 55 });
    const { tg, pinCount, unpinCount, lastPinned, lastUnpinned } = makeTg(99);
    await updateActivePin(registry, tg, CHAT);
    expect(pinCount()).toBe(1);
    expect(lastPinned()).toBe(99);
    expect(unpinCount()).toBe(1);
    expect(lastUnpinned()).toBe(55);
    expect(registry.getActivePin()).toEqual({ chatId: CHAT, messageId: 99 });
  });

  test("called twice for same active → pin called twice (caller must dedupe)", async () => {
    // updateActivePin 자체는 호출 횟수를 제한하지 않음.
    // double-pin 방지는 dispatcher에서 handleSlash 래퍼 1곳에서만 호출하는 구조로 보장.
    const registry = new Registry();
    registry.create("alpha");
    const { tg, sendCount, pinCount } = makeTg(100);
    await updateActivePin(registry, tg, CHAT);
    await updateActivePin(registry, tg, CHAT);
    expect(sendCount()).toBe(2);
    expect(pinCount()).toBe(2);
  });

  test("sendMessage 실패 → pin 미호출, registry 미변경", async () => {
    const registry = new Registry();
    registry.create("alpha");
    const failTg: PinTelegramDeps = {
      sendMessage: mock(async () => { throw new Error("network"); }),
      pinMessage: mock(async () => {}),
      unpinMessage: mock(async () => {}),
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
});
