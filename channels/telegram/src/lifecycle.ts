import {
  mkdirSync,
  readFileSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import * as anomaly from "./anomaly.ts";

const PID_FILE = join(homedir(), ".claude-bridge", "telegram", "bot.pid");
const ORPHAN_POLL_MS = 5_000;
const STALE_WAIT_MS = 2_000;
const FORCE_EXIT_MS = 2_000;

function isAlive(pid: number): boolean {
  if (!pid || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

export async function acquirePollingLock(who: string): Promise<void> {
  mkdirSync(dirname(PID_FILE), { recursive: true });
  try {
    const existing = Number(readFileSync(PID_FILE, "utf-8").trim());
    if (existing && existing !== process.pid && isAlive(existing)) {
      try {
        process.kill(existing, "SIGTERM");
      } catch {}
      const deadline = Date.now() + STALE_WAIT_MS;
      while (Date.now() < deadline) {
        if (!isAlive(existing)) break;
        await new Promise((r) => setTimeout(r, 100));
      }
      if (isAlive(existing)) {
        try {
          process.kill(existing, "SIGKILL");
        } catch {}
      }
      anomaly.log("stale_instance_evicted", { pid: existing, who });
    }
  } catch {}
  writeFileSync(PID_FILE, String(process.pid));
}

export function releasePollingLock(): void {
  try {
    const existing = Number(readFileSync(PID_FILE, "utf-8").trim());
    if (existing === process.pid) unlinkSync(PID_FILE);
  } catch {}
}

type ShutdownFn = (reason: string) => void | Promise<void>;

export function installShutdownHandlers(onShutdown: ShutdownFn): void {
  let triggered = false;
  const run = (reason: string): void => {
    if (triggered) return;
    triggered = true;
    Promise.resolve(onShutdown(reason))
      .catch((err) =>
        anomaly.log("anomaly_self_error", {
          where: "shutdown",
          reason,
          error: String(err),
        }),
      )
      .finally(() => {
        setTimeout(() => process.exit(0), FORCE_EXIT_MS).unref();
      });
  };
  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"] as const) {
    process.on(sig, () => run(sig));
  }
  process.stdin.on("end", () => run("stdin_end"));
  process.stdin.on("close", () => run("stdin_close"));
}

export function startOrphanWatchdog(
  onOrphan: (reason: string) => void,
): NodeJS.Timeout {
  const bootPpid = process.ppid;
  const timer = setInterval(() => {
    if (process.stdin.destroyed) {
      anomaly.log("orphan_detected", { reason: "stdin_destroyed", bootPpid });
      onOrphan("stdin_destroyed");
      return;
    }
    const currentPpid = process.ppid;
    if (currentPpid !== bootPpid || currentPpid === 1) {
      anomaly.log("orphan_detected", {
        reason: "ppid_change",
        bootPpid,
        currentPpid,
      });
      onOrphan("ppid_change");
    }
  }, ORPHAN_POLL_MS);
  timer.unref();
  return timer;
}
