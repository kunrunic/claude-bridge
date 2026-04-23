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

  test("active session, no existing pin → no-op (pin managed by switchActivePin)", async () => {
    const registry = new Registry();
    registry.create("alpha");
    const { tg, sendCount, pinCount, unpinCount } = makeTg(100);
    await updateActivePin(registry, tg, CHAT);
    expect(sendCount()).toBe(0);
    expect(pinCount()).toBe(0);
    expect(unpinCount()).toBe(0);
    expect(registry.getActivePin()).toBeUndefined();
  });

  test("active session, existing pin → no-op (pin managed by switchActivePin)", async () => {
    const registry = new Registry();
    registry.create("alpha");
    registry.setActivePin({ chatId: CHAT, messageId: 55 });
    const { tg, sendCount, pinCount, unpinCount } = makeTg(99);
    await updateActivePin(registry, tg, CHAT);
    expect(sendCount()).toBe(0);
    expect(pinCount()).toBe(0);
    expect(unpinCount()).toBe(0);
    expect(tg.editMessage).not.toHaveBeenCalled();
    expect(registry.getActivePin()).toEqual({ chatId: CHAT, messageId: 55 });
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
