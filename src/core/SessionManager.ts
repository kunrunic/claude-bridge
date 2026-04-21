import { existsSync } from "node:fs";
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
const DIALOG_TIMEOUT_MS = 15_000;
const SPAWN_TIMEOUT_MS = 60_000;
const TRUST_DIALOG_MARKER = "I trust this folder";
const DEV_CHANNEL_WARNING_MARKER = "Loading development channels";
const THEME_DIALOG_MARKER = "Choose the text style";
const GRACEFUL_EXIT_WAIT_MS = 5_000;
const GRACEFUL_EXIT_POLL_MS = 200;

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
};

export class SessionManager {
  private pickerCache: SessionInfo[] = [];

  constructor(private readonly deps: SessionManagerDeps) {}

  spawn(opts: core.SpawnOptions = {}): { id: string; label: string } {
    const result = core.spawnSession(
      {
        registry: this.deps.registry,
        tmux: this.deps.tmux,
        cfg: this.deps.spawnCfg,
        onSpawned: (tmuxName, sessionId) => {
          this.confirmStartupDialogs(tmuxName, sessionId);
        },
      },
      opts,
    );
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

  listRecent(): string {
    this.pickerCache = findSessions(8);
    return formatSessionList(this.pickerCache);
  }

  refreshPickerCache(): SessionInfo[] {
    this.pickerCache = findSessions(8);
    return this.pickerCache;
  }

  resume(target: string, fork: boolean, skipPermissions = false): string {
    let info: SessionInfo | undefined;
    if (/^\d+$/.test(target)) {
      const idx = Number(target) - 1;
      if (idx < 0 || idx >= this.pickerCache.length) {
        return `pick index out of range. run /resume first to refresh the list.`;
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
    if (!info) return `no such session: ${target}`;
    const cwd =
      getSessionCwd(info.id) ?? this.deps.spawnCfg.botWorkspaceDir;
    if (!existsSync(cwd)) {
      return `session cwd missing on disk: ${cwd}`;
    }
    try {
      const spawnOpts: core.SpawnOptions = { cwd, resumeId: info.id, skipPermissions };
      if (info.project) spawnOpts.label = info.project;
      if (fork) spawnOpts.forkSession = true;
      const r = this.spawn(spawnOpts);
      const verb = fork ? "forked" : "resumed";
      return `${verb} ${r.id} (${r.label}) from ${info.project} · ${info.title}`;
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
    const dismissed = { theme: false, trust: false, devWarn: false };
    const poll = (): void => {
      if (Date.now() > deadline) return;
      let pane = "";
      try {
        pane = this.deps.tmux.capturePane(tmuxName, 40);
      } catch {
        setTimeout(poll, DIALOG_POLL_MS);
        return;
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
      if (dismissed.devWarn) return;
      setTimeout(poll, DIALOG_POLL_MS);
    };
    setTimeout(poll, DIALOG_POLL_MS);
  }
}
