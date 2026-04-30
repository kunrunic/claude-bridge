/**
 * cb 클라이언트 측 호스트 관리.
 *
 * 자체 config (~/.cb/hosts.json) 가 source of truth — cb 가 자기 도구로 관리.
 * ssh config 는 동시 갱신 옵션 — 등록 시 ~/.ssh/config 에도 Host 항목 추가하여
 * 일반 `ssh <name>` 도 동작.
 *
 * 자체 config 형식:
 * {
 *   "version": 1,
 *   "hosts": {
 *     "laptop": { "host": "192.168.1.10", "port": 22, "user": "kunrunic", "key": "~/.ssh/id_ed25519" },
 *     "work":   { "host": "work.local",   "port": 2222, "user": "me",       "key": "~/.ssh/id_ed25519" }
 *   }
 * }
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync, chmodSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";

export type HostEntry = {
  host: string;
  port?: number;
  user?: string;
  key?: string;
};

export type HostsConfig = {
  version: 1;
  hosts: Record<string, HostEntry>;
};

const CB_DIR = process.env.CB_CLIENT_HOME ?? join(homedir(), ".cb");
const HOSTS_PATH = join(CB_DIR, "hosts.json");

const RESERVED_NAMES = new Set([
  "add", "list", "remove", "rm", "help", "-h", "--help",
  "connect", "co", "edit",
]);

function ensureCbDir(): void {
  if (!existsSync(CB_DIR)) {
    mkdirSync(CB_DIR, { recursive: true });
    chmodSync(CB_DIR, 0o700);
  } else {
    const mode = statSync(CB_DIR).mode & 0o777;
    if (mode !== 0o700) chmodSync(CB_DIR, 0o700);
  }
}

export function loadHosts(): HostsConfig {
  if (!existsSync(HOSTS_PATH)) {
    return { version: 1, hosts: {} };
  }
  const raw = JSON.parse(readFileSync(HOSTS_PATH, "utf-8"));
  if (!raw || typeof raw !== "object") {
    return { version: 1, hosts: {} };
  }
  return {
    version: 1,
    hosts: (raw.hosts ?? {}) as Record<string, HostEntry>,
  };
}

export function saveHosts(cfg: HostsConfig): void {
  ensureCbDir();
  writeFileSync(HOSTS_PATH, JSON.stringify(cfg, null, 2), { mode: 0o600 });
  chmodSync(HOSTS_PATH, 0o600);
}

export function getHost(name: string): HostEntry | undefined {
  return loadHosts().hosts[name];
}

export function isReservedName(name: string): boolean {
  return RESERVED_NAMES.has(name);
}

/**
 * 호스트 이름 → ssh 인자 배열로 변환.
 *  ["-p", "2222", "-i", "/path/to/key", "user@host"]
 * 비어있는 옵션은 생략.
 */
export function toSshArgs(entry: HostEntry): string[] {
  const args: string[] = [];
  if (entry.port && entry.port !== 22) {
    args.push("-p", String(entry.port));
  }
  if (entry.key) {
    args.push("-i", expandHome(entry.key));
  }
  const target = entry.user ? `${entry.user}@${entry.host}` : entry.host;
  args.push(target);
  return args;
}

function expandHome(p: string): string {
  if (p === "~") return homedir();
  if (p.startsWith("~/")) return join(homedir(), p.slice(2));
  return p;
}

// ── ssh config 갱신 ─────────────────────────────────────────────────────────

const SSH_CONFIG_PATH = join(homedir(), ".ssh", "config");
const CB_BLOCK_BEGIN = "# === cb (claude-bridge) — managed block ===";
const CB_BLOCK_END = "# === end cb ===";

/**
 * ~/.ssh/config 에 cb 가 관리하는 블록을 갱신. 사용자가 직접 작성한 항목은
 * 건드리지 않고, BEGIN/END 마커 사이만 새로 작성.
 */
export function syncSshConfig(cfg: HostsConfig): void {
  const sshDir = dirname(SSH_CONFIG_PATH);
  if (!existsSync(sshDir)) {
    mkdirSync(sshDir, { recursive: true });
    chmodSync(sshDir, 0o700);
  }
  const original = existsSync(SSH_CONFIG_PATH)
    ? readFileSync(SSH_CONFIG_PATH, "utf-8")
    : "";
  const stripped = stripCbBlock(original);
  const block = renderCbBlock(cfg);
  const next = stripped.length === 0 || stripped.endsWith("\n")
    ? stripped + block
    : stripped + "\n" + block;
  writeFileSync(SSH_CONFIG_PATH, next, { mode: 0o600 });
  chmodSync(SSH_CONFIG_PATH, 0o600);
}

function stripCbBlock(content: string): string {
  const beginIdx = content.indexOf(CB_BLOCK_BEGIN);
  if (beginIdx < 0) return content;
  const endIdx = content.indexOf(CB_BLOCK_END, beginIdx);
  if (endIdx < 0) return content;
  // remove the block + trailing newline if any
  const before = content.slice(0, beginIdx);
  const after = content.slice(endIdx + CB_BLOCK_END.length);
  const cleaned = before + after.replace(/^\n/, "");
  return cleaned.replace(/\n{3,}$/, "\n");
}

function renderCbBlock(cfg: HostsConfig): string {
  const entries = Object.entries(cfg.hosts);
  if (entries.length === 0) {
    return `${CB_BLOCK_BEGIN}\n# (no hosts registered)\n${CB_BLOCK_END}\n`;
  }
  const lines: string[] = [CB_BLOCK_BEGIN];
  for (const [name, h] of entries) {
    lines.push(`Host ${name}`);
    lines.push(`  HostName ${h.host}`);
    if (h.port && h.port !== 22) lines.push(`  Port ${h.port}`);
    if (h.user) lines.push(`  User ${h.user}`);
    if (h.key) lines.push(`  IdentityFile ${h.key}`);
    // EscapeChar none — Ctrl-b 같은 control key 가 ssh client 에 가로채지 않고
    // tmux 까지 통과. cb-menu 의 tmux prefix(C-b d) 가 정상 동작하려면 필수.
    lines.push(`  EscapeChar none`);
    lines.push(""); // blank line between hosts
  }
  lines.push(CB_BLOCK_END);
  return lines.join("\n") + "\n";
}

export const PATHS = {
  cbDir: CB_DIR,
  hostsPath: HOSTS_PATH,
  sshConfigPath: SSH_CONFIG_PATH,
};
