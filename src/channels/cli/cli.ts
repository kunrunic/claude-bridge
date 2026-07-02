#!/usr/bin/env bun
/**
 * cb — claude-bridge CLI (클라이언트).
 *
 * 사용자 노트북/원격 디바이스에서 호출하는 도구. dispatcher 가 떠 있는 호스트에
 * SSH 로 접속해서 mdwiz 패턴 + 우리 UI(status bar minimap, popup) 흐름을 자동 진입.
 *
 * 명령:
 *   cb                       사용 가능한 호스트 목록 + 도움말
 *   cb <name>                connect — 등록된 호스트로 ssh + cb-menu attach
 *   cb add [<name>]          호스트 등록 (대화형 default, 플래그도 지원)
 *   cb list                  등록된 호스트 목록
 *   cb remove <name>         등록 해제
 *   cb start <name>          원격 dispatcher 시작
 *   cb stop <name>           원격 dispatcher 정지
 *   cb restart <name>        원격 dispatcher 재시작
 *   cb help                  도움말
 *
 * 설계:
 *  · 자체 config (~/.cb/hosts.json) 가 source of truth
 *  · ssh config (~/.ssh/config) 도 동시 갱신 → `ssh <name>` 도 동작
 *  · cb <name> 은 ssh -t <name> 로 진입 후 영속 cb-menu tmux session 에 직접
 *    attach. dispatcher 가 미리 cb-menu 를 띄워둔 상태라 별도 메뉴 프로세스 spawn 없음.
 */

import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline/promises";
import { fileURLToPath } from "node:url";
import {
  loadHosts,
  saveHosts,
  getHost,
  isReservedName,
  isLocalHost,
  toSshArgs,
  syncSshConfig,
  type HostEntry,
} from "./hosts.ts";

function fail(msg: string, code = 1): never {
  console.error(msg);
  process.exit(code);
}

/**
 * child process 를 stdio inherit 로 spawn 하고, 종료되면 그 exit code 로 cb
 * 프로세스도 종료한다. ssh / bash / tmux TUI 를 그대로 사용자 터미널에 인계.
 */
function spawnInherit(cmd: string, args: string[]): Promise<never> {
  const child = spawn(cmd, args, { stdio: "inherit" });
  return new Promise<never>((_resolve, reject) => {
    child.on("exit", (code) => process.exit(code ?? 0));
    child.on("error", (e) => reject(new Error(`${cmd} failed: ${String(e)}`)));
  });
}

// ── ssh 진입 ────────────────────────────────────────────────────────────────

/**
 * 원격지 진입 명령. dispatcher 가 영속으로 띄워둔 `cb-menu` tmux session 에
 * 직접 attach. 메뉴 안에서 사용자 키 입력 → switch-client 로 다른 cb-* 세션으로
 * 점프하는 흐름. 별도 cb-tui Node 프로세스 spawn 없음 (= stdin 인계 버그 회피).
 *
 * Fallback:
 *  - cb-menu session 부재 시: dispatcher 가 안 떠 있는 것으로 간주 → 안내 출력
 *  - tmux 부재 시: tmux 미설치 안내
 *
 * 원격 login shell 이 zsh / bash / sh 중 무엇이든 안전하게 파싱되도록 `if/then/fi`
 * 대신 `||` + `{ ... }` 한 줄 형식. ssh 가 명령을 한 줄 string 으로 login shell 에
 * 넘기는데, `if ; then ; fi` 구조를 한 줄로 합치면 zsh 가 `;` 위치를 거부하는 경우가
 * 있어 회피한다.
 *
 * SSH non-interactive 가 ~/.zshrc 안 읽는 문제를 회피하기 위해 PATH 보강.
 * CB_INSTANCE 환경변수가 있으면 menu 이름이 cb-<inst>-menu.
 */
const REMOTE_ENTRY_CMD = [
  'PATH="$HOME/.local/bin:/opt/homebrew/bin:$HOME/.bun/bin:$PATH"',
  'MENU_NAME="cb${CB_INSTANCE:+-$CB_INSTANCE}-menu"',
  'command -v tmux >/dev/null 2>&1 || { echo "tmux 가 호스트에 설치돼 있지 않습니다." >&2; exit 1; }',
  'tmux has-session -t "=$MENU_NAME" 2>/dev/null || { echo "cb-menu 세션($MENU_NAME) 미가동 — dispatcher 가 켜져 있는지 확인:" >&2; echo "    ./bin/start.sh" >&2; exit 1; }',
  // start.sh 가 이미 set 한 옵션들 — dispatcher 우회 부팅 케이스 대비 멱등 재설정.
  // mouse on 으로 모든 터미널에서 wheel → copy-mode 일관 동작. Native text select 는
  // iTerm/wezterm 등에서 Option(⌥) + 드래그.
  'tmux set -g  extended-keys on 2>/dev/null || true',
  'tmux set -g  history-limit 50000 2>/dev/null || true',
  'tmux set -g  mouse on 2>/dev/null || true',
  'tmux set -as terminal-features "*:extkeys" 2>/dev/null || true',
  'tmux set -wg mode-keys vi 2>/dev/null || true',
  'exec tmux attach-session -t "=$MENU_NAME"',
].join("; ");

// cli.ts 실제 위치 기준 repo root (src/channels/cli/cli.ts → ../../..).
const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

/** 셸에 안전하게 넘기기 위한 작은따옴표 quoting. */
function shq(s: string): string {
  return `'${s.replace(/'/g, "'\\''")}'`;
}

/**
 * 로컬 connect 전용 진입 명령. cb-menu 세션이 없으면 dispatcher 를 자동 기동하고
 * (bin/start.sh), 세션이 뜰 때까지 최대 ~10초 대기한 뒤 REMOTE_ENTRY_CMD 로 attach.
 * 원격은 ssh 재접속이 얽혀 자동 기동이 애매하므로 로컬(localhost)에만 적용한다.
 */
const LOCAL_ENTRY_CMD = [
  'MENU_NAME="cb${CB_INSTANCE:+-$CB_INSTANCE}-menu"',
  'command -v tmux >/dev/null 2>&1 || { echo "tmux 가 설치돼 있지 않습니다." >&2; exit 1; }',
  'if ! tmux has-session -t "=$MENU_NAME" 2>/dev/null; then',
  '  echo "dispatcher/cb-menu 미가동 — 자동 시작 중..." >&2',
  `  bash ${shq(join(REPO_ROOT, "bin", "start.sh"))} || { echo "✗ dispatcher 자동 시작 실패" >&2; exit 1; }`,
  '  for _i in $(seq 1 50); do tmux has-session -t "=$MENU_NAME" 2>/dev/null && break; sleep 0.2; done',
  '  tmux has-session -t "=$MENU_NAME" 2>/dev/null || { echo "✗ cb-menu 가 시간 내 뜨지 않음 — 로그 확인" >&2; exit 1; }',
  'fi',
  REMOTE_ENTRY_CMD,
].join("\n");

async function cmdConnect(name: string): Promise<never> {
  const entry = getHost(name);
  if (!entry) {
    fail(
      `unknown host: ${name}\n` +
        `등록된 호스트:\n` +
        Object.keys(loadHosts().hosts).map((n) => `  · ${n}`).join("\n") +
        `\n호스트 등록: cb add ${name}`,
    );
  }
  // 로컬 호스트(localhost/127.0.0.1/::1)면 ssh 를 건너뛰고 로컬 login shell 에서
  // 바로 tmux attach. ssh 인증(키/비번) · sshd 없이 `cb <name>` 하나로 붙고,
  // cb-menu 가 없으면 dispatcher 를 자동 기동한 뒤 attach (LOCAL_ENTRY_CMD).
  if (isLocalHost(entry)) {
    return spawnInherit("bash", ["-c", LOCAL_ENTRY_CMD]);
  }
  // -e none: ssh escape character 비활성. default(~) 가 newline 후 첫 char 만 처리하지만,
  //   line discipline / pty mode 에 따라 일부 control 키 (Ctrl-b 포함) 가 ssh client 에서
  //   가로채져 tmux prefix 까지 도달 못 하는 케이스가 있다. 비활성으로 전부 통과시킴.
  // -t : force tty (tmux + Claude TUI 가 interactive pty 필요).
  // StrictHostKeyChecking=accept-new : 첫 연결 시 호스트키 자동 수락 (이후엔 검증).
  //   사용자가 cb add 시점에 검증 없이 추가하므로 첫 ssh 도 동일 수준으로 자동.
  const args = [
    "-e", "none",
    "-o", "StrictHostKeyChecking=accept-new",
    "-t",
    ...toSshArgs(entry),
    REMOTE_ENTRY_CMD,
  ];
  return spawnInherit("ssh", args);
}

// ── add (대화형 + 플래그) ────────────────────────────────────────────────────

type AddOpts = {
  name?: string;
  host?: string;
  port?: number;
  user?: string;
  key?: string;
};

function parseAddFlags(args: string[]): AddOpts {
  const opts: AddOpts = {};
  for (const a of args) {
    if (a.startsWith("--host=")) opts.host = a.slice("--host=".length);
    else if (a.startsWith("--port=")) opts.port = Number(a.slice("--port=".length));
    else if (a.startsWith("--user=")) opts.user = a.slice("--user=".length);
    else if (a.startsWith("--key=")) opts.key = a.slice("--key=".length);
    else if (!a.startsWith("--") && opts.name === undefined) opts.name = a;
  }
  return opts;
}

async function cmdAdd(args: string[]): Promise<void> {
  const opts = parseAddFlags(args);
  const interactive = process.stdin.isTTY === true;
  const rl = interactive
    ? createInterface({ input: process.stdin, output: process.stdout })
    : null;

  /**
   * 대화형이면 사용자에게 묻고 default 적용. 비대화형이면 default 만 사용.
   * default 없는 필수 항목인데 비대화형이면 즉시 에러.
   */
  const ask = async (q: string, deflt?: string): Promise<string> => {
    if (!interactive || !rl) {
      if (deflt !== undefined) return deflt;
      fail(`'${q}' 필수 — 비대화형 환경에서는 플래그로 주세요 (--<key>=...)`);
    }
    const prompt = deflt !== undefined ? `${q} [${deflt}]: ` : `${q}: `;
    const v = (await rl.question(prompt)).trim();
    return v.length === 0 ? (deflt ?? "") : v;
  };

  try {
    const name = opts.name ?? (await ask("name (alias)")).trim();
    if (!name) fail("name 필수");
    if (isReservedName(name)) fail(`'${name}' 은 예약어 — 다른 이름 사용`);

    const host = opts.host ?? (await ask("host (IP / domain)"));
    if (!host) fail("host 필수");

    const portStr =
      opts.port !== undefined ? String(opts.port) : await ask("port", "22");
    const port = Number(portStr);
    if (!Number.isFinite(port) || port < 1 || port > 65535) fail("port 가 유효하지 않음");

    const user = opts.user ?? (await ask("user", process.env.USER ?? ""));
    const defaultKey = guessDefaultKey();
    const key = opts.key ?? (await ask("key path", defaultKey));

    const entry: HostEntry = { host };
    if (port !== 22) entry.port = port;
    if (user) entry.user = user;
    if (key) entry.key = key;

    const cfg = loadHosts();
    cfg.hosts[name] = entry;
    saveHosts(cfg);
    syncSshConfig(cfg);

    console.log(`✓ added '${name}' → ${user ? user + "@" : ""}${host}${port !== 22 ? ":" + port : ""}`);
    console.log(`  ssh config 갱신 — 'ssh ${name}' 도 동작`);
    console.log(`  접속: cb ${name}`);
  } finally {
    rl?.close();
  }
}

function guessDefaultKey(): string {
  const candidates = [
    "~/.ssh/id_ed25519",
    "~/.ssh/id_ecdsa",
    "~/.ssh/id_rsa",
  ];
  for (const c of candidates) {
    const expanded = c.replace("~", homedir());
    try {
      readFileSync(expanded);
      return c;
    } catch {
      // not exists
    }
  }
  return "";
}

// ── start / stop / restart ──────────────────────────────────────────────────

async function cmdDispatcherControl(
  action: "start" | "stop" | "restart",
  name: string,
): Promise<void> {
  const entry = getHost(name);
  if (!entry) {
    fail(
      `unknown host: ${name}\n` +
        `등록된 호스트: ${Object.keys(loadHosts().hosts).join(", ")}`,
    );
  }

  // repo 위치 결정: 1순위 `cb` 심링크 역추적 → 2순위 알려진 폴더 후보 탐색.
  // if/then/fi 여러 줄을 포함하므로 '; ' 가 아니라 newline 으로 이어야 한다
  // ('if ...; then' 뒤에 '; ' 가 붙으면 'then;' 이 되어 bash/zsh 문법 오류).
  // ssh · 로컬 bash -c 모두 multiline script 를 정상 실행한다.
  const remoteCmd = [
    'PATH="$HOME/.bun/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"',
    'DIR=""',
    'CB="$(command -v cb 2>/dev/null)"',
    'if [ -n "$CB" ]; then',
    '  CB_REAL="$(readlink "$CB" 2>/dev/null || echo "$CB")"',
    '  case "$CB_REAL" in /*) ;; *) CB_REAL="$(dirname "$CB")/$CB_REAL" ;; esac',
    '  CAND="$(cd "$(dirname "$(dirname "$CB_REAL")")" 2>/dev/null && pwd)"',
    '  [ -n "$CAND" ] && [ -f "$CAND/bin/start.sh" ] && DIR="$CAND"',
    'fi',
    'if [ -z "$DIR" ]; then',
    '  for d in ~/claude-bridge ~/claude-bridge2 ~/claude-bridge3; do [ -f "$d/bin/start.sh" ] && DIR="$d" && break; done',
    'fi',
    '[ -z "$DIR" ] && { echo "✗ claude-bridge 디렉터리를 찾을 수 없음 (cb 미설치 + 폴더 탐색 실패)" >&2; exit 1; }',
    'cd "$DIR"',
    `bash bin/${action}.sh`,
  ].join("\n");

  // 로컬 호스트면 ssh 없이 로컬에서 바로 dispatcher 스크립트 실행. remoteCmd 는
  // cb 심링크 역추적으로 repo 를 찾으므로 로컬 login shell 에서도 그대로 동작한다.
  if (isLocalHost(entry)) {
    return spawnInherit("bash", ["-c", remoteCmd]);
  }
  const args = [
    "-o", "StrictHostKeyChecking=accept-new",
    ...toSshArgs(entry),
    remoteCmd,
  ];
  return spawnInherit("ssh", args);
}

// ── list / remove / help ────────────────────────────────────────────────────

function cmdList(): void {
  const cfg = loadHosts();
  const entries = Object.entries(cfg.hosts);
  if (entries.length === 0) {
    console.log("등록된 호스트 없음.");
    console.log("  cb add <name>");
    return;
  }
  // 표 형태
  const rows = entries.map(([name, h]) => {
    const target = `${h.user ? h.user + "@" : ""}${h.host}`;
    const port = String(h.port ?? 22);
    const key = h.key ?? "";
    return { name, target, port, key };
  });
  const colName = Math.max(4, ...rows.map((r) => r.name.length));
  const colTarget = Math.max(7, ...rows.map((r) => r.target.length));
  const colPort = Math.max(4, ...rows.map((r) => r.port.length));
  console.log(
    "NAME".padEnd(colName) + "  " +
      "TARGET".padEnd(colTarget) + "  " +
      "PORT".padEnd(colPort) + "  KEY",
  );
  console.log("─".repeat(colName + colTarget + colPort + 6 + 20));
  for (const r of rows) {
    console.log(
      r.name.padEnd(colName) + "  " +
        r.target.padEnd(colTarget) + "  " +
        r.port.padEnd(colPort) + "  " + r.key,
    );
  }
}

function cmdRemove(name: string | undefined): void {
  if (!name) fail("usage: cb remove <name>");
  const cfg = loadHosts();
  if (!(name in cfg.hosts)) fail(`unknown host: ${name}`);
  delete cfg.hosts[name];
  saveHosts(cfg);
  syncSshConfig(cfg);
  console.log(`✓ removed '${name}' (ssh config 도 갱신됨)`);
}

function printHelp(): void {
  console.log([
    "cb — claude-bridge client",
    "",
    "Usage:",
    "  cb                       호스트 목록 + 도움말",
    "  cb <name>                등록된 호스트 접속 (예: cb laptop)",
    "  cb add [<name>] [...]    호스트 등록 (대화형 default; --host=, --port=, --user=, --key= 플래그)",
    "  cb list                  등록된 호스트 목록",
    "  cb remove <name>         등록 해제",
    "  cb start <name>          원격 dispatcher 시작",
    "  cb stop <name>           원격 dispatcher 정지",
    "  cb restart <name>        원격 dispatcher 재시작",
    "  cb help                  도움말",
    "",
    "원격지에 'cb' 라는 명령은 없음 — 접속하면 자동으로 cb-menu (tmux session) 에 attach.",
    "",
    "Config:",
    "  ~/.cb/hosts.json         자체 config (source of truth)",
    "  ~/.ssh/config            ssh config (cb 가 동시 갱신, `ssh <name>` 도 동작)",
  ].join("\n"));
}

// ── entrypoint ──────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  const argv = process.argv.slice(2);
  const cmd = argv[0];

  if (!cmd) {
    cmdList();
    console.log("\n  cb help    더 보기");
    return;
  }

  switch (cmd) {
    case "help":
    case "-h":
    case "--help":
      printHelp();
      return;
    case "add":
      await cmdAdd(argv.slice(1));
      return;
    case "list":
      cmdList();
      return;
    case "remove":
    case "rm":
      cmdRemove(argv[1]);
      return;
    case "connect":
    case "co": {
      // alias for cb <name>
      const name = argv[1];
      if (!name) fail("usage: cb connect <name>");
      await cmdConnect(name);
      return;
    }
    case "start":
    case "stop":
    case "restart": {
      const hostName = argv[1];
      if (!hostName) fail(`usage: cb ${cmd} <name>`);
      await cmdDispatcherControl(cmd, hostName);
      return;
    }
    default:
      // 첫 인자 = 호스트 이름 (smart routing)
      await cmdConnect(cmd);
  }
}

main().catch((e) => {
  console.error(String(e));
  process.exit(1);
});
