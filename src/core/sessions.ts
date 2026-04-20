import { readdirSync, readFileSync, statSync, existsSync } from "node:fs";
import { homedir } from "node:os";
import { basename, join } from "node:path";
import * as anomaly from "./anomaly.ts";

const PROJECTS_DIR = join(homedir(), ".claude", "projects");

export type SessionInfo = {
  id: string;
  project: string;
  title: string;
  last: string;
  mtime: number;
  mtimeText: string;
};

function extractText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    for (const item of content) {
      if (item && typeof item === "object" && (item as { type?: string }).type === "text") {
        const t = (item as { text?: string }).text;
        if (typeof t === "string") return t;
      }
    }
  }
  return "";
}

function isMeaningful(text: string): boolean {
  if (!text) return false;
  const t = text.trim();
  if (t.length < 15) return false;
  if (t.startsWith("<")) return false;
  if (t.startsWith("/") && !t.includes(" ")) return false;
  return true;
}

type ParsedSession = { first: string; last: string; lastTs: number };

function parseSessionJsonl(path: string): ParsedSession {
  let first = "";
  let last = "";
  let lastTs = 0;
  try {
    const raw = readFileSync(path, "utf-8");
    for (const line of raw.split("\n")) {
      if (!line) continue;
      try {
        const d = JSON.parse(line) as {
          type?: string;
          timestamp?: string;
          message?: { content?: unknown };
        };
        if (d.timestamp) {
          const ts = Date.parse(d.timestamp);
          if (!Number.isNaN(ts) && ts > lastTs) lastTs = ts;
        }
        if (d.type !== "user") continue;
        const text = extractText(d.message?.content ?? "");
        if (!isMeaningful(text)) continue;
        const stripped = text.trim();
        if (!first) first = stripped;
        last = stripped;
      } catch {}
    }
  } catch {}
  return { first, last, lastTs };
}

function formatMtime(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function findSessions(limit = 8): SessionInfo[] {
  if (!existsSync(PROJECTS_DIR)) return [];
  const results: SessionInfo[] = [];
  let projects: string[] = [];
  try {
    projects = readdirSync(PROJECTS_DIR);
  } catch (err) {
    anomaly.log("anomaly_self_error", {
      where: "sessions.findSessions.readProjects",
      error: String(err),
    });
    return [];
  }

  for (const proj of projects) {
    const projDir = join(PROJECTS_DIR, proj);
    let files: string[] = [];
    try {
      files = readdirSync(projDir).filter((f) => f.endsWith(".jsonl"));
    } catch {
      continue;
    }
    for (const f of files) {
      if (f.startsWith("agent-")) continue;
      const id = basename(f, ".jsonl");
      const path = join(projDir, f);
      let mtime = 0;
      try {
        mtime = statSync(path).mtimeMs;
      } catch {
        continue;
      }
      const parsed = parseSessionJsonl(path);
      if (!parsed.first) continue;
      const activityMs = parsed.lastTs > 0 ? parsed.lastTs : mtime;
      const projSlug = proj.replace(/^-+/, "");
      const projShort = projSlug.split("-").pop() ?? "";
      results.push({
        id,
        project: projShort,
        title: parsed.first.slice(0, 40),
        last: parsed.last.slice(0, 40),
        mtime: activityMs,
        mtimeText: formatMtime(activityMs),
      });
    }
  }

  results.sort((a, b) => b.mtime - a.mtime);
  return results.slice(0, limit);
}

export function getSessionCwd(sessionId: string): string | undefined {
  if (!existsSync(PROJECTS_DIR)) return undefined;
  let projects: string[] = [];
  try {
    projects = readdirSync(PROJECTS_DIR);
  } catch {
    return undefined;
  }
  for (const proj of projects) {
    const candidate = join(PROJECTS_DIR, proj, `${sessionId}.jsonl`);
    if (!existsSync(candidate)) continue;
    try {
      const raw = readFileSync(candidate, "utf-8");
      for (const line of raw.split("\n")) {
        if (!line) continue;
        try {
          const d = JSON.parse(line) as { cwd?: string };
          if (d.cwd) return d.cwd;
        } catch {}
      }
    } catch {}
  }
  return undefined;
}

export function formatSessionList(list: SessionInfo[]): string {
  if (list.length === 0) return "no recent sessions found";
  const lines = ["Recent Claude sessions:"];
  list.forEach((s, i) => {
    lines.push(`${i + 1}. [${s.mtimeText}] ${s.project} · ${s.title}`);
  });
  lines.push("", "/resume N to continue the same session-id");
  lines.push("/fork N to start a new session-id with inherited context");
  return lines.join("\n");
}
