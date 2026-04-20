import { describe, expect, test, afterAll } from "bun:test";
import * as tmux from "../src/tmux/session.ts";

const TEST_NAME = `cb-test-${process.pid}`;

describe("tmux wrapper", () => {
  afterAll(() => {
    tmux.killSession(TEST_NAME);
  });

  test("new-session + has-session + kill-session", () => {
    expect(tmux.hasSession(TEST_NAME)).toBe(false);
    tmux.newSession({
      name: TEST_NAME,
      command: "sh -c 'while :; do sleep 1; done'",
    });
    expect(tmux.hasSession(TEST_NAME)).toBe(true);
    tmux.killSession(TEST_NAME);
    expect(tmux.hasSession(TEST_NAME)).toBe(false);
  });

  test("send-keys + capture-pane", async () => {
    tmux.newSession({
      name: TEST_NAME,
      command: "cat",
    });
    tmux.sendKeys(TEST_NAME, "hello-from-test");
    await new Promise((r) => setTimeout(r, 300));
    const pane = tmux.capturePane(TEST_NAME, 10);
    expect(pane).toContain("hello-from-test");
    tmux.killSession(TEST_NAME);
  });
});
