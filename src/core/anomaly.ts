import {
  appendFileSync,
  mkdirSync,
  statSync,
  renameSync,
  existsSync,
  readFileSync,
} from "node:fs";
import { paths } from "./paths.ts";

const ROOT = paths.home;
const LOG_PATH = paths.anomalyLog;
const MAX_BYTES = 5 * 1024 * 1024;
const KEEP = 3;

function rotateIfNeeded(): void {
  if (!existsSync(LOG_PATH)) return;
  const size = statSync(LOG_PATH).size;
  if (size < MAX_BYTES) return;
  for (let i = KEEP; i >= 1; i--) {
    const src = i === 1 ? LOG_PATH : `${LOG_PATH}.${i - 1}`;
    const dst = `${LOG_PATH}.${i}`;
    if (existsSync(src)) {
      try {
        renameSync(src, dst);
      } catch {
        // silent — rotation failures must not affect bot logic
      }
    }
  }
}

export type AnomalyKind =
  | "channel_reply_failed"
  | "permission_timeout"
  | "session_spawn_failed"
  | "inbound_no_active_session"
  | "busy_timeout"
  | "compact_error"
  | "mcp_unknown_method"
  | "status_panel_edit_failed"
  | "telegram_api_failed"
  | "tmux_capture_failed"
  | "token_collision_detected"
  | "anomaly_self_error"
  | "shutdown"
  | "stale_instance_evicted"
  | "orphan_detected"
  | "rate_limit_hit"
  | "server_startup"
  | "ipc_connect_start"
  | "ipc_connect_ok"
  | "ipc_connect_failed"
  | "ipc_hello_received"
  | "ipc_spawn_request"
  | "dispatcher_announce_start"
  | "dispatcher_announce_ok"
  | "dispatcher_announce_skip"
  | "dispatcher_announce_give_up"
  | "reply_missing_remind"
  | "reply_missing_remind_failed"
  | "reply_missing_user_notice"
  | "cb_menu_spawn_failed";

export function log(kind: AnomalyKind, ctx: Record<string, unknown> = {}): void {
  try {
    mkdirSync(ROOT, { recursive: true });
    rotateIfNeeded();
    const entry = JSON.stringify({
      ts: new Date().toISOString(),
      kind,
      ...ctx,
    });
    appendFileSync(LOG_PATH, entry + "\n", { encoding: "utf-8" });
  } catch {
    // silent by design — observability failure must not cascade
  }
}

export function summary(windowMs: number): {
  total: number;
  byKind: Map<string, number>;
  lastTs: Map<string, string>;
} {
  const byKind = new Map<string, number>();
  const lastTs = new Map<string, string>();
  let total = 0;
  if (!existsSync(LOG_PATH)) {
    return { total, byKind, lastTs };
  }
  const cutoff = Date.now() - windowMs;
  let raw = "";
  try {
    raw = readFileSync(LOG_PATH, "utf-8");
  } catch {
    return { total, byKind, lastTs };
  }
  for (const line of raw.split("\n")) {
    if (!line) continue;
    try {
      const entry = JSON.parse(line) as { ts?: string; kind?: string };
      if (!entry.ts || !entry.kind) continue;
      if (Date.parse(entry.ts) < cutoff) continue;
      byKind.set(entry.kind, (byKind.get(entry.kind) ?? 0) + 1);
      lastTs.set(entry.kind, entry.ts);
      total += 1;
    } catch {
      // skip malformed line
    }
  }
  return { total, byKind, lastTs };
}

export const LOG_FILE_PATH = LOG_PATH;
