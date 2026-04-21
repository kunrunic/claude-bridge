import { resolve } from "node:path";
import { homedir } from "node:os";

export const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024;
export const TG_TEXT_LIMIT = 4096;

/**
 * Split text into chunks of maximum length.
 * Pure function: no dependencies on external state.
 */
export function chunkByLength(s: string, limit: number): string[] {
  if (s.length <= limit) return [s];
  const out: string[] = [];
  for (let i = 0; i < s.length; i += limit) {
    out.push(s.slice(i, i + limit));
  }
  return out;
}

/**
 * Split text into chunks preferring newline boundaries.
 * Falls back to hard split if a single line exceeds limit.
 * Pure function: no dependencies on external state.
 */
export function chunkByNewline(s: string, limit: number): string[] {
  if (s.length <= limit) return [s];
  const out: string[] = [];
  const lines = s.split(/(\n)/);
  let buf = "";
  for (const part of lines) {
    if (buf.length + part.length > limit) {
      if (buf) out.push(buf);
      if (part.length > limit) {
        for (let i = 0; i < part.length; i += limit) {
          const slice = part.slice(i, i + limit);
          if (i + limit >= part.length) {
            buf = slice;
          } else {
            out.push(slice);
            buf = "";
          }
        }
      } else {
        buf = part;
      }
    } else {
      buf += part;
    }
  }
  if (buf) out.push(buf);
  return out.length ? out : [""];
}

const PROTECTED_PREFIXES = [
  resolve(homedir(), ".claude-bridge"),
  resolve(homedir(), ".claude", "channels"),
];

/**
 * Ensure the path is not a protected system directory.
 * Throws if the path is under .claude-bridge or .claude/channels.
 */
export function assertSendable(absPath: string): void {
  const p = resolve(absPath);
  for (const root of PROTECTED_PREFIXES) {
    if (p === root || p.startsWith(root + "/")) {
      throw new Error(`refuse to send protected path: ${p}`);
    }
  }
}
