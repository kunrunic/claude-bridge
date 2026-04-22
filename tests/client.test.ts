import { describe, expect, test } from "bun:test";
import { TelegramClient } from "../src/channels/telegram/client.ts";

// Stubs the grammy api methods so we can simulate a hanging request without
// any network. Tests verify the timeout + retry logic, not grammy itself.
function stubApi(client: TelegramClient, method: string, impl: () => Promise<unknown>): void {
  const api = client.bot.api as unknown as Record<string, () => Promise<unknown>>;
  api[method] = impl;
}

// Short retry delays so tests complete quickly. Production uses [0, 2s, 5s].
const FAST_RETRY = [0, 10, 20];
// Single attempt for tests that only need to verify timeout once.
const NO_RETRY = [0];

describe("TelegramClient timeout + retry", () => {
  test("sendMessage rejects with timeout error when underlying call hangs", async () => {
    const client = new TelegramClient("fake-token", 150, NO_RETRY);
    stubApi(client, "sendMessage", () => new Promise(() => {}));

    const start = Date.now();
    let err: unknown;
    try {
      await client.sendMessage("chat1", "hi");
    } catch (e) {
      err = e;
    }
    const elapsed = Date.now() - start;

    expect(err).toBeDefined();
    expect(String(err)).toMatch(/sendMessage timeout after 150ms/);
    expect(elapsed).toBeGreaterThanOrEqual(130);
    expect(elapsed).toBeLessThan(500);
  });

  test("sendMessage resolves normally when underlying call is fast", async () => {
    const client = new TelegramClient("fake-token", 1_000, NO_RETRY);
    stubApi(client, "sendMessage", async () => ({ message_id: 42 }));

    const id = await client.sendMessage("chat1", "hi");
    expect(id).toBe(42);
  });

  test("pinMessage also honors the timeout", async () => {
    const client = new TelegramClient("fake-token", 100, NO_RETRY);
    stubApi(client, "pinChatMessage", () => new Promise(() => {}));

    let err: unknown;
    try {
      await client.pinMessage("chat1", 1);
    } catch (e) {
      err = e;
    }
    expect(String(err)).toMatch(/pinChatMessage timeout/);
  });

  test("editMessage rejects on timeout", async () => {
    const client = new TelegramClient("fake-token", 100, NO_RETRY);
    stubApi(client, "editMessageText", () => new Promise(() => {}));

    let err: unknown;
    try {
      await client.editMessage("chat1", 1, "new text");
    } catch (e) {
      err = e;
    }
    expect(String(err)).toMatch(/editMessageText timeout/);
  });

  test("sendMessage retries on transient failure, succeeds on 2nd attempt", async () => {
    const client = new TelegramClient("fake-token", 1_000, FAST_RETRY);
    let calls = 0;
    stubApi(client, "sendMessage", async () => {
      calls++;
      if (calls < 2) throw new Error("transient network error");
      return { message_id: 99 };
    });

    const id = await client.sendMessage("chat1", "hi");
    expect(id).toBe(99);
    expect(calls).toBe(2);
  });

  test("sendMessage gives up after all retry attempts fail", async () => {
    const client = new TelegramClient("fake-token", 100, FAST_RETRY);
    let calls = 0;
    stubApi(client, "sendMessage", async () => {
      calls++;
      throw new Error("persistent failure");
    });

    let err: unknown;
    try {
      await client.sendMessage("chat1", "hi");
    } catch (e) {
      err = e;
    }
    expect(String(err)).toMatch(/persistent failure/);
    expect(calls).toBe(FAST_RETRY.length);
  });

  test("non-retriable errors fail fast without retry", async () => {
    const client = new TelegramClient("fake-token", 1_000, FAST_RETRY);
    let calls = 0;
    stubApi(client, "editMessageText", async () => {
      calls++;
      throw new Error("Bad Request: message is not modified: ...");
    });

    // editMessage maps "not modified" to success — no throw.
    await client.editMessage("chat1", 1, "same text");
    expect(calls).toBe(1);
  });

  test("retriable errors retry up to retryDelaysMs length", async () => {
    const client = new TelegramClient("fake-token", 1_000, FAST_RETRY);
    let calls = 0;
    stubApi(client, "editMessageText", async () => {
      calls++;
      throw new Error("Network error");
    });

    let err: unknown;
    try {
      await client.editMessage("chat1", 1, "text");
    } catch (e) {
      err = e;
    }
    expect(err).toBeDefined();
    expect(calls).toBe(FAST_RETRY.length);
  });

  test("unpinMessage propagates error (not swallowed) for caller retry logic", async () => {
    const client = new TelegramClient("fake-token", 100, NO_RETRY);
    stubApi(client, "unpinChatMessage", async () => {
      throw new Error("Bad Request: message to unpin not found");
    });

    let err: unknown;
    try {
      await client.unpinMessage("chat1", 1);
    } catch (e) {
      err = e;
    }
    expect(err).toBeDefined();
    expect(String(err)).toMatch(/message to unpin not found/);
  });
});
