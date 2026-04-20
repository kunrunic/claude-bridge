import { chmodSync, readFileSync, existsSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { z } from "zod";

const ConfigSchema = z.object({
  botToken: z.string().min(10),
  allowlist: z.array(z.string()).default([]),
  defaultChatId: z.string().optional(),
  dumpEnabled: z.boolean().default(false),
});

export type Config = z.infer<typeof ConfigSchema>;

const DEFAULT_PATH = join(homedir(), ".claude-bridge", "config.json");

function hardenPermissions(path: string): void {
  try {
    const dir = dirname(path);
    const dirMode = statSync(dir).mode & 0o777;
    if (dirMode !== 0o700) chmodSync(dir, 0o700);
    const fileMode = statSync(path).mode & 0o777;
    if (fileMode !== 0o600) chmodSync(path, 0o600);
  } catch {}
}

export function loadConfig(path: string = DEFAULT_PATH): Config {
  if (!existsSync(path)) {
    throw new Error(`config not found: ${path}`);
  }
  hardenPermissions(path);
  const raw = JSON.parse(readFileSync(path, "utf-8"));
  const merged = {
    ...raw,
    botToken: process.env.TELEGRAM_BOT_TOKEN ?? raw.botToken,
    dumpEnabled: raw.dumpEnabled ?? Boolean(process.env.CB_DUMP),
  };
  return ConfigSchema.parse(merged);
}
