import { spawnSync, spawn } from "node:child_process";
import * as anomaly from "../anomaly.ts";

const TMUX = "tmux";

function run(args: string[]): { code: number; stdout: string; stderr: string } {
  const res = spawnSync(TMUX, args, { encoding: "utf-8" });
  return {
    code: res.status ?? -1,
    stdout: res.stdout ?? "",
    stderr: res.stderr ?? "",
  };
}

export type SpawnOpts = {
  name: string;
  command: string;
  env?: Record<string, string>;
  cwd?: string;
};

export function hasSession(name: string): boolean {
  const res = run(["has-session", "-t", `=${name}`]);
  return res.code === 0;
}

export function newSession(opts: SpawnOpts): void {
  if (hasSession(opts.name)) {
    throw new Error(`tmux session already exists: ${opts.name}`);
  }
  const args = ["new-session", "-d", "-s", opts.name];
  if (opts.cwd) args.push("-c", opts.cwd);
  for (const [k, v] of Object.entries(opts.env ?? {})) {
    args.push("-e", `${k}=${v}`);
  }
  args.push(opts.command);
  const res = run(args);
  if (res.code !== 0) {
    anomaly.log("session_spawn_failed", {
      name: opts.name,
      stderr: res.stderr,
    });
    throw new Error(`tmux new-session failed: ${res.stderr}`);
  }
}

export function killSession(name: string): void {
  if (!hasSession(name)) return;
  const res = run(["kill-session", "-t", `=${name}`]);
  if (res.code !== 0) {
    anomaly.log("tmux_capture_failed", {
      op: "kill-session",
      name,
      stderr: res.stderr,
    });
  }
}

export function sendKeys(name: string, keys: string, withEnter = true): void {
  const target = `=${name}:`;
  const args = ["send-keys", "-t", target, keys];
  if (withEnter) args.push("Enter");
  const res = run(args);
  if (res.code !== 0) {
    anomaly.log("tmux_capture_failed", {
      op: "send-keys",
      name,
      stderr: res.stderr,
    });
    throw new Error(`tmux send-keys failed: ${res.stderr}`);
  }
}

export function capturePane(name: string, lines = 200): string {
  const target = `=${name}:`;
  const res = run([
    "capture-pane",
    "-p",
    "-t",
    target,
    "-S",
    `-${lines}`,
    "-J",
  ]);
  if (res.code !== 0) {
    anomaly.log("tmux_capture_failed", {
      op: "capture-pane",
      name,
      stderr: res.stderr,
    });
    return "";
  }
  return res.stdout;
}

export function listSessions(prefix?: string): string[] {
  const res = run(["list-sessions", "-F", "#S"]);
  if (res.code !== 0) return [];
  const names = res.stdout.split("\n").filter(Boolean);
  return prefix ? names.filter((n) => n.startsWith(prefix)) : names;
}
