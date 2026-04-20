import { readFileSync, existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { z } from "zod";

const ConfigSchema = z.object({
  botToken: z.string().min(10),
  allowlist: z.array(z.string()).default([]),
  defaultChatId: z.string().optional(),
  dumpEnabled: z.boolean().default(false),
});

export type Config = z.infer<typeof ConfigSchema>;

const DEFAULT_PATH = join(homedir(), ".claude-bridge", "config.json");

export function loadConfig(path: string = DEFAULT_PATH): Config {
  if (!existsSync(path)) {
    throw new Error(`config not found: ${path}`);
  }
  const raw = JSON.parse(readFileSync(path, "utf-8"));
  const merged = {
    ...raw,
    botToken: process.env.TELEGRAM_BOT_TOKEN ?? raw.botToken,
    dumpEnabled: raw.dumpEnabled ?? Boolean(process.env.CB_DUMP),
  };
  return ConfigSchema.parse(merged);
}
