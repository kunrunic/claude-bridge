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
} from "../src/dispatcher-core.ts";
import { Registry } from "../src/registry.ts";
import type { LineSocket } from "../src/ipc.ts";

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
  };
  return state;
}

const cfg: SpawnConfig = {
  channelName: "tg_channel",
  blockedTools: ["x"],
  allowedTools: ["y"],
  socketPath: "/tmp/sock",
  botWorkspaceDir: "/tmp/bot-ws",
};

describe("spawnSession", () => {
  test("creates session, spawns tmux, marks spawning", () => {
    const registry = new Registry();
    const tmux = fakeTmux();
    const r = spawnSession({ registry, tmux, cfg }, "backend");
    expect(r.label).toBe("backend");
    expect(tmux.spawned.length).toBe(1);
    expect(tmux.spawned[0]!.cwd).toBe("/tmp/bot-ws");
    const s = registry.get(r.id)!;
    expect(s.state).toBe("spawning");
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
    spawnSession(deps, "alpha");
    expect(calls.length).toBe(1);
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
  function makeDeps(): HandleSlashDeps & {
    spawnCalls: Array<string | undefined>;
    killCalls: string[];
  } {
    const registry = new Registry();
    const spawnCalls: Array<string | undefined> = [];
    const killCalls: string[] = [];
    const deps: HandleSlashDeps = {
      registry,
      spawn: (label) => {
        spawnCalls.push(label);
        const s = registry.create(label);
        return { id: s.id, label: s.label };
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
    return Object.assign(deps, { spawnCalls, killCalls });
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
    expect(deps.spawnCalls).toEqual(["backend"]);
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

  test("/kill delegates", () => {
    const deps = makeDeps();
    const s = deps.registry.create("alpha");
    const r = handleSlash(deps, { kind: "kill", target: s.id });
    expect(r).toMatch(/killed/);
    expect(deps.killCalls).toEqual([s.id]);
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
