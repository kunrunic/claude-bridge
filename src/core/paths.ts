import { homedir } from "node:os";
import { join } from "node:path";

export const CB_HOME =
  process.env.CB_HOME ?? join(homedir(), ".claude-bridge");

export const paths = {
  home: CB_HOME,
  configPath: join(CB_HOME, "config.json"),
  anomalyLog: join(CB_HOME, "anomaly.jsonl"),
  pidFile: join(CB_HOME, "telegram", "bot.pid"),
  socketPath: join(CB_HOME, "dispatcher.sock"),
  registryPath: join(CB_HOME, "registry.json"),
  workspacesRoot: join(CB_HOME, "workspaces"),
  logsDir: join(CB_HOME, "logs"),
} as const;

export const CB_INSTANCE =
  process.env.CB_INSTANCE ??
  (process.env.CB_HOME ? sanitizeInstance(process.env.CB_HOME) : "");

function sanitizeInstance(value: string): string {
  const base = value.replace(/\/+$/, "").split("/").pop() ?? "";
  return base.replace(/[^a-zA-Z0-9_-]/g, "-").slice(0, 24);
}
