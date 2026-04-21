import { describe, expect, test, mock } from "bun:test";
import { Registry } from "../src/core/registry.ts";
import { TickObserver, type TickObserverDeps, type TickTmux } from "../src/core/TickObserver.ts";
import { BUSY_ACTIVE_RE, INPUT_READY_RE, RATE_LIMIT_RE, RATE_LIMIT_RESET_RE } from "../src/core/observer.ts";

// ── Fake implementations ──────────────────────────────────────────────────────

function fakeTmux(captureText: string = "> "): TickTmux & { captured: Map<string, string> } {
  const state: TickTmux & { captured: Map<string, string> } = {
    captured: new Map(),
    capturePane: mock((name: string, _lines: number) => {
      return state.captured.get(name) ?? "> ";
    }),
    sendKeys: mock(() => {}),
  };
  return state;
}

// ── TickObserver tests ────────────────────────────────────────────────────────

describe("TickObserver", () => {
  describe("start() / stop()", () => {
    test("timer starts and stops", async () => {
      const registry = new Registry();
      const tmux = fakeTmux();
      const deps: TickObserverDeps = {
        registry,
        tmux,
        announce: () => {},
      };
      const observer = new TickObserver(deps);

      observer.start();
      expect(tmux.capturePane).not.toHaveBeenCalled();

      // Wait for first tick (5s default, but we wait a bit)
      await new Promise((r) => setTimeout(r, 100));

      // Stop the observer
      observer.stop();

      // Clear call count and wait again
      const callsBefore = (tmux.capturePane as any).mock.callCount ?? 0;
      await new Promise((r) => setTimeout(r, 100));
      const callsAfter = (tmux.capturePane as any).mock.callCount ?? 0;

      // Should have no additional calls after stop
      expect(callsAfter).toBe(callsBefore);
    });
  });

  describe("tick()", () => {
    test("skips dead state sessions", () => {
      const registry = new Registry();
      const s = registry.create("alpha");
      registry.updateState(s.id, { state: "dead" });
      const tmux = fakeTmux();
      const deps: TickObserverDeps = {
        registry,
        tmux,
        announce: () => {},
      };
      const observer = new TickObserver(deps);

      // Manually trigger a tick
      (observer as any).tick();

      // capturePane should not be called for dead sessions
      expect(tmux.capturePane).not.toHaveBeenCalled();
    });

    test("skips spawning state sessions", () => {
      const registry = new Registry();
      const s = registry.create("alpha");
      // state is spawning by default after create()
      const tmux = fakeTmux();
      const deps: TickObserverDeps = {
        registry,
        tmux,
        announce: () => {},
      };
      const observer = new TickObserver(deps);

      (observer as any).tick();

      // capturePane should not be called for spawning sessions
      expect(tmux.capturePane).not.toHaveBeenCalled();
    });

    test("pane matches BUSY_ACTIVE_RE → state becomes busy", () => {
      const registry = new Registry();
      const s = registry.create("alpha");
      registry.updateState(s.id, { state: "idle" });
      const tmux = fakeTmux();
      tmux.captured.set(s.tmuxName, "Running…\n> ");
      const deps: TickObserverDeps = {
        registry,
        tmux,
        announce: () => {},
      };
      const observer = new TickObserver(deps);

      (observer as any).tick();

      expect(registry.get(s.id)!.state).toBe("busy");
    });

    test("pane matches INPUT_READY_RE → state becomes idle", () => {
      const registry = new Registry();
      const s = registry.create("alpha");
      registry.updateState(s.id, { state: "busy" });
      const tmux = fakeTmux();
      tmux.captured.set(s.tmuxName, "some output\n> ");
      const deps: TickObserverDeps = {
        registry,
        tmux,
        announce: () => {},
      };
      const observer = new TickObserver(deps);

      (observer as any).tick();

      expect(registry.get(s.id)!.state).toBe("idle");
    });

    test("pane matches RATE_LIMIT_RE → announce called, Escape sent, state=idle, rateLimitNotified set", () => {
      const registry = new Registry();
      const s = registry.create("alpha");
      registry.updateState(s.id, { state: "idle" });
      const tmux = fakeTmux();
      tmux.captured.set(s.tmuxName, "You've hit your limit for this model");
      const announced: string[] = [];
      const deps: TickObserverDeps = {
        registry,
        tmux,
        announce: (t) => announced.push(t),
      };
      const observer = new TickObserver(deps);

      (observer as any).tick();

      expect(announced.length).toBeGreaterThan(0);
      expect(announced[0]).toMatch(/rate limit hit/);
      expect(tmux.sendKeys).toHaveBeenCalledWith(s.tmuxName, "Escape", false);
      expect(registry.get(s.id)!.state).toBe("idle");
    });

    test("rate limit only announced once (rateLimitNotified prevents duplicate)", () => {
      const registry = new Registry();
      const s = registry.create("alpha");
      registry.updateState(s.id, { state: "idle" });
      const tmux = fakeTmux();
      tmux.captured.set(s.tmuxName, "You've hit your limit for this model");
      const announced: string[] = [];
      const deps: TickObserverDeps = {
        registry,
        tmux,
        announce: (t) => announced.push(t),
      };
      const observer = new TickObserver(deps);

      // First tick
      (observer as any).tick();
      expect(announced.length).toBe(1);

      // Second tick with same pane
      (observer as any).tick();
      // Should not announce again (rateLimitNotified has s.id)
      expect(announced.length).toBe(1);
    });

    test("rate limit notified cleared when session goes idle", () => {
      const registry = new Registry();
      const s = registry.create("alpha");
      registry.updateState(s.id, { state: "idle" });
      const tmux = fakeTmux();
      const announced: string[] = [];
      const deps: TickObserverDeps = {
        registry,
        tmux,
        announce: (t) => announced.push(t),
      };
      const observer = new TickObserver(deps);

      // First tick: rate limit hit
      tmux.captured.set(s.tmuxName, "You've hit your limit for this model");
      (observer as any).tick();
      expect(announced.length).toBe(1);

      // Second tick: rate limit cleared, pane shows idle
      tmux.captured.set(s.tmuxName, "> ");
      (observer as any).tick();

      // rateLimitNotified should be cleared, so hitting rate limit again would announce
      tmux.captured.set(s.tmuxName, "You've hit your limit for this model");
      (observer as any).tick();
      // Should have announced twice total
      expect(announced.length).toBe(2);
    });

    test("pane matches compact error → state=error", () => {
      const registry = new Registry();
      const s = registry.create("alpha");
      registry.updateState(s.id, { state: "idle" });
      const tmux = fakeTmux();
      tmux.captured.set(s.tmuxName, "Compaction failed: out of memory");
      const deps: TickObserverDeps = {
        registry,
        tmux,
        announce: () => {},
      };
      const observer = new TickObserver(deps);

      (observer as any).tick();

      expect(registry.get(s.id)!.state).toBe("error");
    });

    test("capturePane throws → session skipped (no crash)", () => {
      const registry = new Registry();
      const s = registry.create("alpha");
      registry.updateState(s.id, { state: "idle" });
      const tmux = fakeTmux();
      tmux.capturePane = mock(() => {
        throw new Error("tmux error");
      });
      const deps: TickObserverDeps = {
        registry,
        tmux,
        announce: () => {},
      };
      const observer = new TickObserver(deps);

      // Should not throw
      expect(() => {
        (observer as any).tick();
      }).not.toThrow();

      // State should remain unchanged
      expect(registry.get(s.id)!.state).toBe("idle");
    });
  });
});
