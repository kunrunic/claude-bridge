import { startServer, type IpcMessage } from "../src/core/ipc.ts";
import * as tmux from "../src/core/tmux/session.ts";
import { unlinkSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { tmpdir, homedir } from "node:os";

const socketPath = join(tmpdir(), `cb-smoke-${process.pid}.sock`);
const sessionName = `cb-smoke-${process.pid}`;
const received: IpcMessage[] = [];

try {
  unlinkSync(socketPath);
} catch {}

const server = startServer(socketPath, (ls) => {
  console.log("[smoke] client connected");
  ls.onMessage((m) => {
    console.log("[smoke] recv:", m.op);
    received.push(m);
  });
});

console.log("[smoke] ipc server at", socketPath);

const DENY = [
  "mcp__plugin_telegram_telegram__reply",
  "mcp__plugin_telegram_telegram__react",
  "mcp__plugin_telegram_telegram__edit_message",
  "mcp__plugin_telegram_telegram__download_attachment",
].join(",");
const ALLOW = [
  "mcp__tg_channel__reply",
  "mcp__tg_channel__react",
  "mcp__tg_channel__edit_message",
  "mcp__tg_channel__download_attachment",
].join(",");

const botWorkspaceDir = join(homedir(), ".claude-bridge", "workspaces", "bot");
mkdirSync(botWorkspaceDir, { recursive: true });

tmux.newSession({
  name: sessionName,
  command: `claude --disallowedTools ${DENY} --allowedTools ${ALLOW} --dangerously-load-development-channels --channels server:tg_channel`,
  cwd: botWorkspaceDir,
  env: {
    CB_DISPATCHER_SOCKET: socketPath,
    CB_SESSION_ID: "smoke-s1",
    CB_POLL_DISABLED: "1",
  },
});

console.log("[smoke] tmux session spawned:", sessionName);

const trustDeadline = Date.now() + 10_000;
while (Date.now() < trustDeadline) {
  const pane = tmux.capturePane(sessionName, 40);
  if (pane.includes("I trust this folder")) {
    tmux.sendKeys(sessionName, "", true);
    console.log("[smoke] Enter sent to confirm trust dialog");
    break;
  }
  await new Promise((r) => setTimeout(r, 200));
}

const warnDeadline = Date.now() + 10_000;
while (Date.now() < warnDeadline) {
  const pane = tmux.capturePane(sessionName, 40);
  if (pane.includes("WARNING: Loading development channels")) {
    tmux.sendKeys(sessionName, "", true);
    console.log("[smoke] Enter sent to dismiss dev warning");
    break;
  }
  await new Promise((r) => setTimeout(r, 200));
}

const deadline = Date.now() + 15_000;
while (Date.now() < deadline && received.find((m) => m.op === "hello") == null) {
  await new Promise((r) => setTimeout(r, 500));
}

const pane = tmux.capturePane(sessionName, 40);
console.log("[smoke] pane tail:\n" + pane);
console.log("[smoke] received ops:", received.map((m) => m.op));

// happy-path checks:
//  1) MCP server -> dispatcher hello round-tripped
//  2) Claude Code accepted channel mode (listening banner present)
//  3) 'server: entries need --dangerously-load-development-channels' NOT shown
//     — that banner is the smoking-gun that channel mode is disabled and
//     inbound notifications would be silently dropped into the void.
const helloOk = received.find((m) => m.op === "hello") != null;
const listeningBanner = pane.includes("Listening for channel messages");
const devChannelsBroken = pane.includes("server: entries need --dangerously-load-development-channels");

console.log("[smoke] hello received:        ", helloOk);
console.log("[smoke] listening banner:      ", listeningBanner);
console.log("[smoke] channel mode disabled: ", devChannelsBroken);

tmux.killSession(sessionName);
server.close();
try {
  unlinkSync(socketPath);
} catch {}

if (!helloOk) {
  console.error("[smoke] FAIL: dispatcher never received hello");
  process.exit(1);
}
if (!listeningBanner) {
  console.error("[smoke] FAIL: Claude Code never printed channel listening banner");
  process.exit(1);
}
if (devChannelsBroken) {
  console.error(
    "[smoke] FAIL: channel mode disabled — MCP tools connect but inbound " +
      "notifications are dropped. did you remove --dangerously-load-development-channels?",
  );
  process.exit(1);
}

console.log("[smoke] PASS: spawn → hello → channel listening OK");
process.exit(0);
