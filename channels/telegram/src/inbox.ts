import { mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, extname } from "node:path";
import * as anomaly from "./anomaly.ts";

const INBOX_ROOT = join(homedir(), ".claude-bridge", "inbox");

export function inboxDateDir(): string {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const dir = join(INBOX_ROOT, `${y}${m}${day}`);
  mkdirSync(dir, { recursive: true });
  return dir;
}

const SAFE_EXT_RE = /^\.[a-z0-9]{1,8}$/i;

export async function saveAttachment(
  botToken: string,
  fileId: string,
  telegramFilePath: string,
): Promise<string> {
  const url = `https://api.telegram.org/file/bot${botToken}/${telegramFilePath}`;
  const res = await fetch(url);
  if (!res.ok) {
    const err = `attachment fetch failed: ${res.status} ${res.statusText}`;
    anomaly.log("telegram_api_failed", { op: "getFile.download", fileId, error: err });
    throw new Error(err);
  }
  const buf = new Uint8Array(await res.arrayBuffer());
  const rawExt = extname(telegramFilePath).toLowerCase();
  const ext = SAFE_EXT_RE.test(rawExt) ? rawExt : "";
  const outPath = join(inboxDateDir(), `${fileId}${ext}`);
  writeFileSync(outPath, buf);
  return outPath;
}
