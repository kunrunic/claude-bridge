import { existsSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import type { Registry } from "./registry.ts";
import type { LineSocket } from "./ipc.ts";
import * as core from "./dispatcher-core.ts";
import { scheduleSpawnTimeout } from "./dispatcher-handlers.ts";
import {
  findSessions,
  formatSessionList,
  getSessionCwd,
  type SessionInfo,
} from "./sessions.ts";
import * as anomaly from "./anomaly.ts";

const DIALOG_POLL_MS = 200;
// Resume/fork of a large session (e.g. 170k tokens, 6h old) can take tens of
// seconds before the "Resume from summary" dialog appears. Keep polling long
// enough to catch it. Polling is cheap (tmux capturePane at 200ms interval).
const DIALOG_TIMEOUT_MS = 60_000;
const SPAWN_TIMEOUT_MS = 60_000;
const TRUST_DIALOG_MARKER = "I trust this folder";
const DEV_CHANNEL_WARNING_MARKER = "Loading development channels";
const THEME_DIALOG_MARKER = "Choose the text style";
// "Select login method" shows when claude has no auth token. We can't auto-
// dismiss — user must log in manually on their own machine. Fail fast here
// instead of waiting for the 60s hello timeout.
const LOGIN_DIALOG_MARKER = "Select login method";
// Appears when resuming a session with large token count — option 1 is already
// selected by default, so sending Enter confirms "Resume from summary".
const RESUME_SUMMARY_MARKER = "Resume from summary (recommended)";
const GRACEFUL_EXIT_WAIT_MS = 5_000;
const GRACEFUL_EXIT_POLL_MS = 200;

function expandHome(p: string): string {
  if (p === "~") return homedir();
  if (p.startsWith("~/")) return `${homedir()}${p.slice(1)}`;
  return p;
}

// tmux subset needed by SessionManager
export type SessionTmux = core.TmuxDriver & {
  capturePane(name: string, lines: number): string;
  sendKeys(name: string, keys: string, enter: boolean): void;
};

export type SessionManagerDeps = {
  registry: Registry;
  tmux: SessionTmux;
  sockets: Map<string, LineSocket>;
  spawnCfg: core.SpawnConfig;
  announce: (text: string) => void;
  /**
   * native 모드 spawn 시 호출. bridge 모드에서는 호출되지 않으며, 대신 hello
   * IPC 도착 후 dispatcher 가 같은 의미의 ready 처리를 한다.
   *
   * native 모드는 MCP 가 없어 hello IPC 가 영원히 안 오므로, spawn 직후 즉시
   * 세션을 ready 로 간주하고 채널들에 SessionEvent("spawned") fan-out 한다.
   */
  onSpawnFinalized?: (sessionId: string) => void;
};

export class SessionManager {
  private pickerCache: SessionInfo[] = [];

  constructor(private readonly deps: SessionManagerDeps) {}

  spawn(opts: core.SpawnOptions = {}): { id: string; label: string } {
    const expanded: core.SpawnOptions = { ...opts };
    if (expanded.cwd) {
      expanded.cwd = expandHome(expanded.cwd);
      // Auto-create the workspace directory if it doesn't exist — user's /new
      // with a path implies intent to start working there.
      if (!existsSync(expanded.cwd)) {
        try {
          mkdirSync(expanded.cwd, { recursive: true });
        } catch (err) {
          throw new Error(`failed to create cwd ${expanded.cwd}: ${String(err)}`);
        }
      }
    }
    const result = core.spawnSession(
      {
        registry: this.deps.registry,
        tmux: this.deps.tmux,
        cfg: this.deps.spawnCfg,
        onSpawned: (tmuxName, sessionId) => {
          this.confirmStartupDialogs(tmuxName, sessionId);
        },
      },
      expanded,
    );
    if (this.deps.spawnCfg.mode === "bridge") {
      // hello IPC 도착까지 대기. 시간 초과 시 spawning → error 처리.
      scheduleSpawnTimeout(
        this.deps.registry,
        result.id,
        result.label,
        SPAWN_TIMEOUT_MS,
        this.deps.announce,
        () => {
          const s = this.deps.registry.get(result.id);
          if (s) {
            try { this.deps.tmux.killSession(s.tmuxName); } catch {}
            this.deps.registry.remove(result.id);
          }
        },
      );
    } else {
      // native 모드: hello 가 절대 안 오므로 즉시 idle + ready 콜백.
      // TickObserver 가 signal 갱신 (입력 prompt 표시되면 idle, 작업 중이면 busy).
      this.deps.registry.updateState(result.id, { state: "idle" });
      this.deps.onSpawnFinalized?.(result.id);
    }
    return result;
  }

  kill(target: string): boolean {
    return core.killSession(
      {
        registry: this.deps.registry,
        tmux: this.deps.tmux,
        sockets: this.deps.sockets,
      },
      target,
    );
  }

  private filterResumable(sessions: SessionInfo[]): SessionInfo[] {
    return sessions.filter((s) => {
      const cwd = getSessionCwd(s.id) ?? this.deps.spawnCfg.botWorkspaceDir;
      return existsSync(cwd);
    });
  }

  listRecent(): string {
    this.pickerCache = this.filterResumable(findSessions(8));
    return formatSessionList(this.pickerCache);
  }

  refreshPickerCache(limit = 8): SessionInfo[] {
    this.pickerCache = this.filterResumable(findSessions(limit));
    return this.pickerCache;
  }

  resume(target: string, fork: boolean, skipPermissions = false, autoSwitch = true): string {
    let info: SessionInfo | undefined;
    if (/^\d+$/.test(target)) {
      const idx = Number(target) - 1;
      if (idx < 0 || idx >= this.pickerCache.length) {
        return `⚠️ 목록 범위 초과 — /resume 다시 열어 목록을 새로 불러오세요`;
      }
      info = this.pickerCache[idx];
    } else {
      info = this.pickerCache.find(
        (s) => s.id === target || s.id.startsWith(target),
      );
      if (!info) {
        const all = findSessions(64);
        info = all.find((s) => s.id === target || s.id.startsWith(target));
      }
    }
    if (!info) return `⚠️ 해당 ID 세션 없음: ${target}`;
    const cwd =
      getSessionCwd(info.id) ?? this.deps.spawnCfg.botWorkspaceDir;
    if (!existsSync(cwd)) {
      return `⚠️ 세션 cwd 가 디스크에 없음: ${cwd}`;
    }
    try {
      const source = `${info.project} · ${info.title}`;
      const spawnOpts: core.SpawnOptions = { cwd, resumeId: info.id, skipPermissions, source };
      if (info.project) spawnOpts.label = info.project;
      if (fork) spawnOpts.forkSession = true;
      if (!autoSwitch) spawnOpts.autoSwitch = false;
      const r = this.spawn(spawnOpts);
      const verb = fork ? "forking" : "resuming";
      // Terse reply — full origin (source) is surfaced in the "자동 전환됨"
      // announce once the MCP hello lands, to keep a single clean message.
      return `${verb} [${r.id}][${r.label}]...`;
    } catch (err) {
      return `spawn failed: ${String(err)}`;
    }
  }

  async gracefulKill(tmuxName: string): Promise<void> {
    if (!this.deps.tmux.hasSession(tmuxName)) return;
    try {
      this.deps.tmux.sendKeys(tmuxName, "/exit", true);
    } catch {
      // if send-keys fails, fall through to force kill.
    }
    const deadline = Date.now() + GRACEFUL_EXIT_WAIT_MS;
    while (Date.now() < deadline) {
      if (!this.deps.tmux.hasSession(tmuxName)) return;
      await new Promise((r) => setTimeout(r, GRACEFUL_EXIT_POLL_MS));
    }
    try {
      this.deps.tmux.killSession(tmuxName);
    } catch {
      // already gone or tmux server died — ignore
    }
  }

  private confirmStartupDialogs(tmuxName: string, sessionId: string): void {
    const deadline = Date.now() + DIALOG_TIMEOUT_MS;
    const dismissed = { theme: false, trust: false, devWarn: false, resumeSummary: false };
    const poll = (): void => {
      if (Date.now() > deadline) return;
      let pane = "";
      try {
        pane = this.deps.tmux.capturePane(tmuxName, 40);
      } catch {
        setTimeout(poll, DIALOG_POLL_MS);
        return;
      }
      // Login method screen can't be auto-dismissed — fail fast and instruct
      // the user. Kill the tmux window so they don't have to close it manually.
      if (pane.includes(LOGIN_DIALOG_MARKER)) {
        const session = this.deps.registry.get(sessionId);
        const label = session?.label ?? sessionId;
        this.deps.announce(
          `🔐 [${sessionId}][${label}] 재인증 필요 — 사용 기기에서 claude 를 실행해 로그인한 뒤 요청하신 작업을 다시 시도하세요`,
        );
        anomaly.log("session_spawn_failed", {
          op: "login-dialog-detected",
          session: sessionId,
          reason: "auth_required",
        });
        try {
          this.deps.tmux.killSession(tmuxName);
        } catch {
          // best-effort — registry cleanup below is what matters for state
        }
        this.deps.registry.remove(sessionId);
        return;
      }
      if (!dismissed.resumeSummary && pane.includes(RESUME_SUMMARY_MARKER)) {
        try {
          this.deps.tmux.sendKeys(tmuxName, "", true);
          dismissed.resumeSummary = true;
        } catch (err) {
          anomaly.log("session_spawn_failed", {
            op: "resume-summary-dialog-confirm",
            session: sessionId,
            error: String(err),
          });
        }
      }
      if (!dismissed.theme && pane.includes(THEME_DIALOG_MARKER)) {
        try {
          this.deps.tmux.sendKeys(tmuxName, "", true);
          dismissed.theme = true;
        } catch (err) {
          anomaly.log("session_spawn_failed", {
            op: "theme-dialog-confirm",
            session: sessionId,
            error: String(err),
          });
        }
      }
      if (!dismissed.trust && pane.includes(TRUST_DIALOG_MARKER)) {
        try {
          this.deps.tmux.sendKeys(tmuxName, "", true);
          dismissed.trust = true;
        } catch (err) {
          anomaly.log("session_spawn_failed", {
            op: "trust-dialog-confirm",
            session: sessionId,
            error: String(err),
          });
        }
      }
      if (!dismissed.devWarn && pane.includes(DEV_CHANNEL_WARNING_MARKER)) {
        try {
          this.deps.tmux.sendKeys(tmuxName, "", true);
          dismissed.devWarn = true;
        } catch (err) {
          anomaly.log("session_spawn_failed", {
            op: "dev-warning-confirm",
            session: sessionId,
            error: String(err),
          });
        }
      }
      // Don't early-terminate on devWarn dismissal — for resume/fork flows
      // the "Resume from summary" dialog appears AFTER the devWarn, often
      // many seconds later while Claude Code loads the session context.
      // Keep polling until deadline.
      setTimeout(poll, DIALOG_POLL_MS);
    };
    setTimeout(poll, DIALOG_POLL_MS);
  }
}
