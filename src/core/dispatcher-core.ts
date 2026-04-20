import { randomBytes } from "node:crypto";
import type { Registry } from "./registry.ts";
import type { SlashCommand } from "./slash.ts";
import type { LineSocket } from "./ipc.ts";

export type TmuxDriver = {
  newSession: (opts: {
    name: string;
    command: string;
    env?: Record<string, string>;
    cwd?: string;
  }) => void;
  killSession: (name: string) => void;
  hasSession: (name: string) => boolean;
};

export type SpawnConfig = {
  channelName: string;
  blockedTools: string[];
  allowedTools: string[];
  socketPath: string;
  botWorkspaceDir: string;
};

export type SpawnDeps = {
  registry: Registry;
  tmux: TmuxDriver;
  cfg: SpawnConfig;
  onSpawned?: (tmuxName: string, sessionId: string) => void;
};

export type SpawnOptions = {
  label?: string;
  cwd?: string;
  resumeId?: string;
  forkSession?: boolean;
};

export function spawnSession(
  deps: SpawnDeps,
  opts: SpawnOptions = {},
): { id: string; label: string } {
  const session = deps.registry.create(opts.label);
  // collision avoidance: if another tmux session already owns the default
  // name (e.g. user-created `cb-s1` or a prior unclean dispatcher crash left
  // it behind), append a random suffix.
  if (deps.tmux.hasSession(session.tmuxName)) {
    const suffix = randomBytes(3).toString("hex");
    const unique = `${session.tmuxName}-${suffix}`;
    deps.registry.updateState(session.id, { tmuxName: unique });
    session.tmuxName = unique;
  }
  const cwd = opts.cwd ?? deps.cfg.botWorkspaceDir;
  try {
    const denyArg = deps.cfg.blockedTools.join(",");
    const allowArg = deps.cfg.allowedTools.join(",");
    const resumeArgs = opts.resumeId
      ? ` --resume ${opts.resumeId}${opts.forkSession ? " --fork-session" : ""}`
      : "";
    deps.tmux.newSession({
      name: session.tmuxName,
      command:
        `claude --disallowedTools ${denyArg} --allowedTools ${allowArg}${resumeArgs} ` +
        `--channels server:${deps.cfg.channelName}`,
      cwd,
      env: {
        CB_DISPATCHER_SOCKET: deps.cfg.socketPath,
        CB_SESSION_ID: session.id,
        CB_POLL_DISABLED: "1",
      },
    });
  } catch (err) {
    deps.registry.remove(session.id);
    throw err;
  }
  deps.registry.updateState(session.id, { state: "spawning" });
  deps.onSpawned?.(session.tmuxName, session.id);
  return { id: session.id, label: session.label };
}

export type KillDeps = {
  registry: Registry;
  tmux: TmuxDriver;
  sockets: Map<string, LineSocket>;
};

export function killSession(deps: KillDeps, target: string): boolean {
  const session =
    deps.registry.get(target) ?? deps.registry.getByLabel(target);
  if (!session) return false;
  deps.tmux.killSession(session.tmuxName);
  const ls = session.socketId ? deps.sockets.get(session.socketId) : undefined;
  ls?.close();
  deps.registry.remove(session.id);
  return true;
}

export type HandleSlashDeps = {
  registry: Registry;
  spawn: (opts: { label?: string; cwd?: string }) => {
    id: string;
    label: string;
  };
  resume: (target: string, fork: boolean) => string;
  listRecent: () => string;
  kill: (target: string) => boolean;
  renderStatus: () => string;
};

export function handleSlash(
  deps: HandleSlashDeps,
  cmd: SlashCommand,
): string {
  switch (cmd.kind) {
    case "sessions": {
      const list = deps.registry.list();
      if (list.length === 0) return "no sessions. /new to start.";
      const active = deps.registry.active();
      return list
        .map((s) => {
          const mark = active && active.id === s.id ? "▶" : "  ";
          return `${mark} ${s.id} (${s.label}) — ${s.state}/${s.signal}`;
        })
        .join("\n");
    }
    case "new": {
      try {
        const opts: { label?: string; cwd?: string } = {};
        if (cmd.label !== undefined) opts.label = cmd.label;
        if (cmd.cwd !== undefined) opts.cwd = cmd.cwd;
        const r = deps.spawn(opts);
        return `spawned ${r.id} (${r.label})`;
      } catch (err) {
        return `spawn failed: ${String(err)}`;
      }
    }
    case "resume": {
      if (!cmd.target) return deps.listRecent();
      return deps.resume(cmd.target, false);
    }
    case "fork": {
      if (!cmd.target) return deps.listRecent();
      return deps.resume(cmd.target, true);
    }
    case "switch": {
      const s =
        deps.registry.get(cmd.target) ??
        deps.registry.getByLabel(cmd.target);
      if (!s) return `no such session: ${cmd.target}`;
      deps.registry.setActive(s.id);
      const missed = s.backlog.length;
      return `▶ switched to ${s.label}${
        missed ? ` · ${missed} backlog msg(s)` : ""
      }`;
    }
    case "kill": {
      return deps.kill(cmd.target)
        ? `killed ${cmd.target}`
        : `no such session: ${cmd.target}`;
    }
    case "current": {
      const a = deps.registry.active();
      return a ? `active: ${a.label}` : "no active session";
    }
    case "backlog": {
      const s = cmd.target
        ? (deps.registry.get(cmd.target) ??
          deps.registry.getByLabel(cmd.target))
        : deps.registry.active();
      if (!s) return "no such session";
      return s.backlog.length === 0
        ? `(${s.label}) no backlog`
        : `(${s.label}) backlog:\n${s.backlog.slice(-20).join("\n")}`;
    }
    case "status": {
      return deps.renderStatus();
    }
  }
}
