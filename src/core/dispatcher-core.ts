import { randomBytes } from "node:crypto";
import { basename } from "node:path";
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

/**
 * Spawn 모드:
 *  - "bridge": MCP server (bridge-channel) + channel system prompt 와 함께
 *    Claude 시작. Telegram 같은 외부 채널이 inbound 메시지를 IPC 로 라우팅하는
 *    전제. Claude 는 reply / react 등 도구를 통해 응답.
 *  - "native": claude TUI 만 단순히 spawn. MCP / channel / prompt 모두 미사용.
 *    사용자가 cb attach (tmux attach) 로 직접 Claude TUI 와 상호작용. CLI-only
 *    운영 모드 (config 에 botToken 없을 때).
 */
export type SpawnMode = "bridge" | "native";

export type SpawnConfig = {
  mode: SpawnMode;
  socketPath: string;
  botWorkspaceDir: string;
  skipPermissions: boolean;
  // mode === "bridge" 일 때만 사용. native 모드면 무시.
  channelName?: string;
  blockedTools?: string[];
  allowedTools?: string[];
  channelPromptFile?: string;
  mcpConfigFile?: string;
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
  skipPermissions?: boolean;
  // Origin summary saved into the registry session (`source`), surfaced in
  // the "자동 전환됨" announce. Used by /resume and /fork flows.
  source?: string;
};

export function spawnSession(
  deps: SpawnDeps,
  opts: SpawnOptions = {},
): { id: string; label: string } {
  const cwd = opts.cwd ?? deps.cfg.botWorkspaceDir;
  const label = opts.label ?? basename(cwd);
  const session = deps.registry.create(label);
  if (opts.source) {
    deps.registry.updateState(session.id, { source: opts.source });
  }
  // collision avoidance: if another tmux session already owns the default
  // name (e.g. user-created `cb-s1` or a prior unclean dispatcher crash left
  // it behind), append a random suffix.
  if (deps.tmux.hasSession(session.tmuxName)) {
    const suffix = randomBytes(3).toString("hex");
    const unique = `${session.tmuxName}-${suffix}`;
    deps.registry.updateState(session.id, { tmuxName: unique });
    session.tmuxName = unique;
  }
  try {
    const resumeArgs = opts.resumeId
      ? ` --resume ${opts.resumeId}${opts.forkSession ? " --fork-session" : ""}`
      : "";
    const skipPerm = opts.skipPermissions ?? deps.cfg.skipPermissions;
    const skipPermArgs = skipPerm ? " --dangerously-skip-permissions" : "";
    let command: string;
    if (deps.cfg.mode === "bridge") {
      const denyArg = (deps.cfg.blockedTools ?? []).join(",");
      const allowArg = (deps.cfg.allowedTools ?? []).join(",");
      command =
        `claude --disallowedTools ${denyArg} --allowedTools ${allowArg}${resumeArgs}${skipPermArgs}` +
        ` --append-system-prompt-file "${deps.cfg.channelPromptFile}"` +
        ` --mcp-config "${deps.cfg.mcpConfigFile}"` +
        ` --dangerously-load-development-channels server:${deps.cfg.channelName}`;
    } else {
      // native: 외부 채널 없이 Claude TUI 만. 사용자가 cb attach 로 직접 사용.
      command = `claude${resumeArgs}${skipPermArgs}`;
    }
    deps.tmux.newSession({
      name: session.tmuxName,
      command,
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
  spawn: (opts: { cwd?: string }) => {
    id: string;
    label: string;
  };
  resume: (target: string, fork: boolean) => string;
  listRecent: () => string;
  kill: (target: string) => boolean;
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
        const opts: { cwd?: string } = {};
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
    case "kill": {
      if (!cmd.target) return "use /kill <id|label>, or /kill with no arg for a session picker";
      return deps.kill(cmd.target)
        ? `killed ${cmd.target}`
        : `no such session: ${cmd.target}`;
    }
  }
}
