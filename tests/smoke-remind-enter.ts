// Smoke test for TickObserver 재전송 요청 Enter 제출 검증.
//
// TickObserver.checkMissingReply 는 tmux.sendKeys 를 텍스트와 Enter 로 분리 호출
// 한다 (긴 UTF-8 텍스트 + Enter 동시 전송 시 bracketed-paste 모드로 처리되어
// Enter 가 literal newline 이 되는 문제 회피용).
//
// unit 테스트는 sendKeys 가 두 번 호출되는 것만 검증 가능하고, 실제 Claude TUI
// 가 그 Enter 를 "제출" 로 해석하는지까지는 확인 못 한다. 이 smoke 는 실제
// claude 바이너리를 spawn 해서 텍스트→Enter 분리 전송 시 Claude 가 제출로
// 받아들여 busy 상태로 전환되는지 확인한다.

import { startServer, type IpcMessage } from "../src/core/ipc.ts";
import * as tmux from "../src/core/tmux/session.ts";
import { existsSync, unlinkSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { tmpdir, homedir } from "node:os";

function resolveClaudeBinary(): string {
  const canonical = join(homedir(), ".local", "bin", "claude");
  if (existsSync(canonical)) return canonical;
  return "claude";
}
const CLAUDE_BIN = resolveClaudeBinary();

const socketPath = join(tmpdir(), `cb-smoke-remind-${process.pid}.sock`);
const sessionName = `cb-smoke-remind-${process.pid}`;
const received: IpcMessage[] = [];

try {
  unlinkSync(socketPath);
} catch {}

const server = startServer(socketPath, (ls) => {
  ls.onMessage((m) => received.push(m));
});

const DENY = [
  "mcp__plugin_telegram_telegram__reply",
  "mcp__plugin_telegram_telegram__react",
  "mcp__plugin_telegram_telegram__edit_message",
  "mcp__plugin_telegram_telegram__download_attachment",
].join(",");
const ALLOW = [
  "mcp__bridge-channel__reply",
  "mcp__bridge-channel__react",
  "mcp__bridge-channel__edit_message",
  "mcp__bridge-channel__download_attachment",
].join(",");

const botWorkspaceDir = join(homedir(), ".claude-bridge", "workspaces", "bot");
mkdirSync(botWorkspaceDir, { recursive: true });

tmux.newSession({
  name: sessionName,
  command: `${CLAUDE_BIN} --disallowedTools ${DENY} --allowedTools ${ALLOW} --dangerously-load-development-channels server:bridge-channel`,
  cwd: botWorkspaceDir,
  env: {
    CB_DISPATCHER_SOCKET: socketPath,
    CB_SESSION_ID: "smoke-remind-s1",
    CB_POLL_DISABLED: "1",
  },
});

console.log("[smoke-remind] tmux session spawned:", sessionName);

async function waitForPane(pattern: RegExp | string, timeoutMs: number): Promise<string | null> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const pane = tmux.capturePane(sessionName, 60);
    const matched =
      typeof pattern === "string" ? pane.includes(pattern) : pattern.test(pane);
    if (matched) return pane;
    await new Promise((r) => setTimeout(r, 300));
  }
  return null;
}

// ── 1. dismiss startup dialogs ───────────────────────────────────────────────
const trustPane = await waitForPane("I trust this folder", 10_000);
if (trustPane) {
  tmux.sendKeys(sessionName, "", true);
  console.log("[smoke-remind] trust dialog dismissed");
}

const warnPane = await waitForPane("WARNING: Loading development channels", 10_000);
if (warnPane) {
  tmux.sendKeys(sessionName, "", true);
  console.log("[smoke-remind] dev warning dismissed");
}

// ── 2. wait for hello + listening banner ─────────────────────────────────────
const helloDeadline = Date.now() + 20_000;
while (Date.now() < helloDeadline && received.find((m) => m.op === "hello") == null) {
  await new Promise((r) => setTimeout(r, 500));
}
if (!received.find((m) => m.op === "hello")) {
  console.error("[smoke-remind] FAIL: hello never received");
  tmux.killSession(sessionName);
  server.close();
  process.exit(1);
}
console.log("[smoke-remind] hello received");

const bannerPane = await waitForPane("Listening for channel messages", 10_000);
if (!bannerPane) {
  console.error("[smoke-remind] FAIL: listening banner never printed");
  tmux.killSession(sessionName);
  server.close();
  process.exit(1);
}
console.log("[smoke-remind] listening banner present");

// ── 3. wait for idle prompt (❯ or >) ────────────────────────────────────────
const idlePane = await waitForPane(/^\s*[❯>]\s*$/m, 10_000);
if (!idlePane) {
  console.error("[smoke-remind] FAIL: Claude never reached idle prompt");
  console.error("[smoke-remind] pane tail:\n" + tmux.capturePane(sessionName, 40));
  tmux.killSession(sessionName);
  server.close();
  process.exit(1);
}
console.log("[smoke-remind] idle prompt reached");

// ── 4. send split text + Enter (the actual behavior under test) ─────────────
const TEST_TEXT =
  "(시스템) smoke 테스트 — 이 메시지가 제출되면 Claude 가 응답을 시작합니다.";
tmux.sendKeys(sessionName, TEST_TEXT, false);
await new Promise((r) => setTimeout(r, 300));
tmux.sendKeys(sessionName, "Enter", false);
console.log("[smoke-remind] text + Enter sent separately");

// ── 5. verify submission: pane should no longer show TEST_TEXT on the
// input prompt line. If Enter was treated as literal newline, the text
// would still be sitting in the input buffer after the ❯ prompt.
await new Promise((r) => setTimeout(r, 3_000));
const afterPane = tmux.capturePane(sessionName, 60);
console.log("[smoke-remind] pane after Enter:\n" + afterPane);

// If Enter was absorbed into the paste buffer, the prompt line still contains
// the text next to the ❯ marker. Check for that smoking-gun pattern.
const textStuckInPrompt = /❯\s.*smoke 테스트/.test(afterPane);

tmux.killSession(sessionName);
server.close();
try {
  unlinkSync(socketPath);
} catch {}

if (textStuckInPrompt) {
  console.error(
    "[smoke-remind] FAIL: text is still in the input prompt — Enter was " +
      "treated as literal newline (bracketed paste) instead of submit.",
  );
  process.exit(1);
}

console.log(
  "[smoke-remind] PASS: split text + Enter submitted cleanly (input prompt clear)",
);
process.exit(0);
