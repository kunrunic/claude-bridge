import { describe, expect, test } from "bun:test";
import {
  handleSlash,
  killSession,
  spawnSession,
  type HandleSlashDeps,
  type KillDeps,
  type SpawnConfig,
  type SpawnDeps,
  type TmuxDriver,
} from "../src/core/dispatcher-core.ts";
import { Registry } from "../src/core/registry.ts";
import type { LineSocket } from "../src/core/ipc.ts";

type FakeTmux = TmuxDriver & {
  spawned: Array<{ name: string; cwd?: string; env?: Record<string, string> }>;
  killed: string[];
  failNext: boolean;
};

function fakeTmux(): FakeTmux {
  const state: FakeTmux = {
    spawned: [],
    killed: [],
    failNext: false,
    newSession: (opts) => {
      if (state.failNext) {
        state.failNext = false;
        throw new Error("tmux fail");
      }
      const entry: { name: string; cwd?: string; env?: Record<string, string> } =
        { name: opts.name };
      if (opts.cwd !== undefined) entry.cwd = opts.cwd;
      if (opts.env !== undefined) entry.env = opts.env;
      state.spawned.push(entry);
    },
    killSession: (name) => {
      state.killed.push(name);
    },
    hasSession: (name) => state.spawned.some((s) => s.name === name),
  };
  return state;
}

const cfg: SpawnConfig = {
  channelName: "tg_channel",
  blockedTools: ["x"],
  allowedTools: ["y"],
  socketPath: "/tmp/sock",
  botWorkspaceDir: "/tmp/bot-ws",
  skipPermissions: false,
  channelPromptFile: "/tmp/channel-prompt.txt",
  mcpConfigFile: "/tmp/mcp.json",
};

describe("spawnSession", () => {
  test("creates session, spawns tmux, marks spawning", () => {
    const registry = new Registry();
    const tmux = fakeTmux();
    const r = spawnSession({ registry, tmux, cfg }, { label: "backend" });
    expect(r.label).toBe("backend");
    expect(tmux.spawned.length).toBe(1);
    expect(tmux.spawned[0]!.cwd).toBe("/tmp/bot-ws");
    const s = registry.get(r.id)!;
    expect(s.state).toBe("spawning");
  });

  test("custom cwd overrides default", () => {
    const registry = new Registry();
    const tmux = fakeTmux();
    spawnSession({ registry, tmux, cfg }, { cwd: "/some/repo" });
    expect(tmux.spawned[0]!.cwd).toBe("/some/repo");
  });

  test("resumeId adds --resume flag", () => {
    const registry = new Registry();
    const tmux = fakeTmux();
    const captured: string[] = [];
    const instrumentedTmux: typeof tmux = Object.assign(tmux, {
      newSession: (opts: { command: string; name: string; cwd?: string; env?: Record<string, string> }) => {
        captured.push(opts.command);
      },
    });
    spawnSession({ registry, tmux: instrumentedTmux, cfg }, {
      resumeId: "abc-123",
    });
    expect(captured[0]).toMatch(/--resume abc-123/);
    expect(captured[0]).not.toMatch(/--fork-session/);
  });

  test("forkSession adds --fork-session when resumeId set", () => {
    const registry = new Registry();
    const tmux = fakeTmux();
    const captured: string[] = [];
    const instrumented = Object.assign(tmux, {
      newSession: (opts: { command: string; name: string; cwd?: string; env?: Record<string, string> }) => {
        captured.push(opts.command);
      },
    });
    spawnSession({ registry, tmux: instrumented, cfg }, {
      resumeId: "abc-123",
      forkSession: true,
    });
    expect(captured[0]).toMatch(/--resume abc-123/);
    expect(captured[0]).toMatch(/--fork-session/);
  });

  test("skipPermissions=true adds --dangerously-skip-permissions flag", () => {
    const registry = new Registry();
    const tmux = fakeTmux();
    const captured: string[] = [];
    const instrumented = Object.assign(tmux, {
      newSession: (opts: { command: string; name: string; cwd?: string; env?: Record<string, string> }) => {
        captured.push(opts.command);
      },
    });
    const skipCfg: SpawnConfig = { ...cfg, skipPermissions: true };
    spawnSession({ registry, tmux: instrumented, cfg: skipCfg });
    expect(captured[0]).toMatch(/--dangerously-skip-permissions/);
  });

  test("skipPermissions=false omits --dangerously-skip-permissions", () => {
    const registry = new Registry();
    const tmux = fakeTmux();
    const captured: string[] = [];
    const instrumented = Object.assign(tmux, {
      newSession: (opts: { command: string; name: string; cwd?: string; env?: Record<string, string> }) => {
        captured.push(opts.command);
      },
    });
    spawnSession({ registry, tmux: instrumented, cfg });
    expect(captured[0]).not.toMatch(/--dangerously-skip-permissions/);
  });

  test("rolls back registry when tmux fails", () => {
    const registry = new Registry();
    const tmux = fakeTmux();
    tmux.failNext = true;
    expect(() => spawnSession({ registry, tmux, cfg })).toThrow(/tmux fail/);
    expect(registry.list().length).toBe(0);
  });

  test("calls onSpawned after successful spawn", () => {
    const registry = new Registry();
    const tmux = fakeTmux();
    const calls: Array<[string, string]> = [];
    const deps: SpawnDeps = {
      registry,
      tmux,
      cfg,
      onSpawned: (tmuxName, sessionId) => calls.push([tmuxName, sessionId]),
    };
    spawnSession(deps, { label: "alpha" });
    expect(calls.length).toBe(1);
  });

  test("tmux name collision triggers random suffix", () => {
    const registry = new Registry();
    const tmux = fakeTmux();
    // pre-occupy the name that would otherwise be generated for s1.
    tmux.spawned.push({ name: "cb-s1" });
    const r = spawnSession({ registry, tmux, cfg }, { label: "squat" });
    const s = registry.get(r.id)!;
    expect(s.tmuxName).not.toBe("cb-s1");
    expect(s.tmuxName.startsWith("cb-s1-")).toBe(true);
    expect(tmux.spawned.some((e) => e.name === s.tmuxName)).toBe(true);
  });
});

describe("killSession", () => {
  test("kills existing session by id", () => {
    const registry = new Registry();
    const tmux = fakeTmux();
    const s = registry.create("alpha");
    const sockets = new Map<string, LineSocket>();
    const ok = killSession({ registry, tmux, sockets }, s.id);
    expect(ok).toBe(true);
    expect(tmux.killed).toEqual([s.tmuxName]);
    expect(registry.get(s.id)).toBeUndefined();
  });

  test("kills by label", () => {
    const registry = new Registry();
    const tmux = fakeTmux();
    registry.create("alpha");
    const ok = killSession(
      { registry, tmux, sockets: new Map() },
      "alpha",
    );
    expect(ok).toBe(true);
  });

  test("returns false for unknown target", () => {
    const registry = new Registry();
    const tmux = fakeTmux();
    const ok = killSession(
      { registry, tmux, sockets: new Map() },
      "ghost",
    );
    expect(ok).toBe(false);
  });
});

describe("handleSlash", () => {
  type TestDeps = HandleSlashDeps & {
    spawnCalls: Array<{ label?: string; cwd?: string }>;
    resumeCalls: Array<{ target: string; fork: boolean }>;
    killCalls: string[];
    listRecentCount: { n: number };
  };

  function makeDeps(): TestDeps {
    const registry = new Registry();
    const spawnCalls: Array<{ label?: string; cwd?: string }> = [];
    const resumeCalls: Array<{ target: string; fork: boolean }> = [];
    const killCalls: string[] = [];
    const listRecentCount = { n: 0 };
    const deps: HandleSlashDeps = {
      registry,
      spawn: (opts) => {
        spawnCalls.push(opts);
        const s = registry.create(opts.label);
        return { id: s.id, label: s.label };
      },
      resume: (target, fork) => {
        resumeCalls.push({ target, fork });
        return fork ? `forked ${target}` : `resumed ${target}`;
      },
      listRecent: () => {
        listRecentCount.n += 1;
        return "RECENT_LIST";
      },
      kill: (target) => {
        killCalls.push(target);
        const s = registry.get(target) ?? registry.getByLabel(target);
        if (!s) return false;
        registry.remove(s.id);
        return true;
      },
      renderStatus: () => "STATUS",
    };
    return Object.assign(deps, {
      spawnCalls,
      resumeCalls,
      killCalls,
      listRecentCount,
    });
  }

  test("/sessions empty", () => {
    const deps = makeDeps();
    const r = handleSlash(deps, { kind: "sessions" });
    expect(r).toMatch(/no sessions/);
  });

  test("/new spawns and reports", () => {
    const deps = makeDeps();
    const r = handleSlash(deps, { kind: "new", label: "backend" });
    expect(r).toMatch(/spawned/);
    expect(deps.spawnCalls).toEqual([{ label: "backend" }]);
  });

  test("/new passes cwd through", () => {
    const deps = makeDeps();
    handleSlash(deps, { kind: "new", label: "backend", cwd: "/tmp/x" });
    expect(deps.spawnCalls[0]).toEqual({ label: "backend", cwd: "/tmp/x" });
  });

  test("/new reports spawn failure cleanly", () => {
    const deps = makeDeps();
    deps.spawn = () => {
      throw new Error("boom");
    };
    const r = handleSlash(deps, { kind: "new" });
    expect(r).toMatch(/spawn failed/);
    expect(r).toMatch(/boom/);
  });

  test("/resume no arg → listRecent", () => {
    const deps = makeDeps();
    const r = handleSlash(deps, { kind: "resume" });
    expect(r).toBe("RECENT_LIST");
    expect(deps.listRecentCount.n).toBe(1);
  });

  test("/resume with target → resume (fork=false)", () => {
    const deps = makeDeps();
    const r = handleSlash(deps, { kind: "resume", target: "2" });
    expect(r).toBe("resumed 2");
    expect(deps.resumeCalls).toEqual([{ target: "2", fork: false }]);
  });

  test("/fork with target → resume (fork=true)", () => {
    const deps = makeDeps();
    const r = handleSlash(deps, { kind: "fork", target: "3" });
    expect(r).toBe("forked 3");
    expect(deps.resumeCalls).toEqual([{ target: "3", fork: true }]);
  });

  test("/switch by label", () => {
    const deps = makeDeps();
    deps.registry.create("alpha");
    deps.registry.create("beta");
    const r = handleSlash(deps, { kind: "switch", target: "beta" });
    expect(r).toMatch(/switched to beta/);
    expect(deps.registry.active()?.label).toBe("beta");
  });

  test("/switch missing target", () => {
    const deps = makeDeps();
    const r = handleSlash(deps, { kind: "switch", target: "nope" });
    expect(r).toMatch(/no such session/);
  });

  test("/kill with target delegates", () => {
    const deps = makeDeps();
    const s = deps.registry.create("alpha");
    const r = handleSlash(deps, { kind: "kill", target: s.id });
    expect(r).toMatch(/killed/);
    expect(deps.killCalls).toEqual([s.id]);
  });

  test("/kill no target → usage hint (keyboard shown at dispatcher layer)", () => {
    const deps = makeDeps();
    deps.registry.create("alpha");
    const r = handleSlash(deps, { kind: "kill" });
    expect(r).toMatch(/kill/);
    expect(deps.killCalls).toHaveLength(0);
  });

  test("/current with active", () => {
    const deps = makeDeps();
    deps.registry.create("alpha");
    const r = handleSlash(deps, { kind: "current" });
    expect(r).toMatch(/alpha/);
  });

  test("/current none", () => {
    const deps = makeDeps();
    const r = handleSlash(deps, { kind: "current" });
    expect(r).toMatch(/no active session/);
  });

  test("/backlog empty", () => {
    const deps = makeDeps();
    deps.registry.create("alpha");
    const r = handleSlash(deps, { kind: "backlog" });
    expect(r).toMatch(/no backlog/);
  });

  test("/backlog populated", () => {
    const deps = makeDeps();
    const s = deps.registry.create("alpha");
    deps.registry.pushBacklog(s.id, "msg-a");
    const r = handleSlash(deps, { kind: "backlog" });
    expect(r).toMatch(/msg-a/);
  });

  test("/status delegates to renderStatus", () => {
    const deps = makeDeps();
    const r = handleSlash(deps, { kind: "status" });
    expect(r).toBe("STATUS");
  });
});
