import { describe, expect, test, mock } from "bun:test";
import { Registry } from "../src/core/registry.ts";
import {
  TickObserver,
  type TickObserverDeps,
  type TickTmux,
  IDLE_GRACE_MS,
  STAGE2_DELAY_MS,
  REMIND_TEXT,
} from "../src/core/TickObserver.ts";
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

    test("busy→idle 전환 시 idleSinceTs 기록", () => {
      const registry = new Registry();
      const s = registry.create("alpha");
      registry.updateState(s.id, { state: "busy" });
      const tmux = fakeTmux();
      tmux.captured.set(s.tmuxName, "some output\n> ");
      const deps: TickObserverDeps = { registry, tmux, announce: () => {} };
      const observer = new TickObserver(deps);

      (observer as any).tick();

      const after = registry.get(s.id)!;
      expect(after.state).toBe("idle");
      expect(after.idleSinceTs).toBeDefined();
      expect(typeof after.idleSinceTs).toBe("number");
    });
  });

  describe("checkMissingReply", () => {
    function prep(): {
      registry: Registry;
      tmux: ReturnType<typeof fakeTmux>;
      announced: string[];
      observer: TickObserver;
      sessionId: string;
      tmuxName: string;
    } {
      const registry = new Registry();
      const s = registry.create("alpha");
      const tmux = fakeTmux();
      tmux.captured.set(s.tmuxName, "> ");
      const announced: string[] = [];
      const deps: TickObserverDeps = {
        registry,
        tmux,
        announce: (t) => announced.push(t),
      };
      const observer = new TickObserver(deps);
      return { registry, tmux, announced, observer, sessionId: s.id, tmuxName: s.tmuxName };
    }

    test("inbound 없으면 발동 안 함", () => {
      const { registry, tmux, observer, sessionId } = prep();
      registry.updateState(sessionId, {
        state: "idle",
        idleSinceTs: Date.now() - (IDLE_GRACE_MS + 5_000),
      });
      (observer as any).tick();
      expect(tmux.sendKeys).not.toHaveBeenCalledWith(
        expect.any(String),
        REMIND_TEXT,
        expect.any(Boolean),
      );
    });

    test("reply 받았으면 발동 안 함 (replySentAt >= inboundAt)", () => {
      const { registry, tmux, observer, sessionId } = prep();
      const now = Date.now();
      registry.updateState(sessionId, {
        state: "idle",
        idleSinceTs: now - (IDLE_GRACE_MS + 5_000),
        inboundAt: now - 30_000,
        replySentAt: now - 20_000,
      });
      (observer as any).tick();
      expect(tmux.sendKeys).not.toHaveBeenCalledWith(
        expect.any(String),
        REMIND_TEXT,
        expect.any(Boolean),
      );
    });

    test("idleSinceTs 가 inboundAt 보다 오래됐으면 발동 안 함 (이전 턴의 stale idle)", () => {
      // 어제 답변 완료 → 오늘 새 inbound 도착 시나리오
      const { registry, tmux, observer, sessionId } = prep();
      const now = Date.now();
      registry.updateState(sessionId, {
        state: "idle",
        idleSinceTs: now - 24 * 60 * 60 * 1_000, // 어제 idle 전환
        inboundAt: now - 30_000,                   // 오늘 새 inbound
        replySentAt: now - (24 * 60 * 60 * 1_000 + 60_000), // 어제 reply
      });
      (observer as any).tick();
      const remindCalls = (tmux.sendKeys as ReturnType<typeof mock>).mock.calls
        .filter((c) => c[1] === REMIND_TEXT);
      expect(remindCalls.length).toBe(0);
    });

    test("1단계 발동 시 텍스트와 Enter 가 별도 tmux 호출로 전송", () => {
      const { registry, tmux, observer, sessionId, tmuxName } = prep();
      const now = Date.now();
      registry.updateState(sessionId, {
        state: "idle",
        idleSinceTs: now - (IDLE_GRACE_MS + 5_000),
        inboundAt: now - 60_000,
      });
      (observer as any).tick();
      const calls = (tmux.sendKeys as ReturnType<typeof mock>).mock.calls;
      const textCall = calls.find((c) => c[0] === tmuxName && c[1] === REMIND_TEXT);
      const enterCall = calls.find((c) => c[0] === tmuxName && c[1] === "Enter");
      expect(textCall).toBeDefined();
      expect(textCall![2]).toBe(false); // Enter 는 텍스트 호출에 포함 안 됨
      expect(enterCall).toBeDefined();
    });

    test("idle grace 미경과 시 발동 안 함", () => {
      const { registry, tmux, observer, sessionId } = prep();
      const now = Date.now();
      registry.updateState(sessionId, {
        state: "idle",
        idleSinceTs: now - 3_000, // way under IDLE_GRACE_MS
        inboundAt: now - 10_000,
      });
      (observer as any).tick();
      expect(tmux.sendKeys).not.toHaveBeenCalledWith(
        expect.any(String),
        REMIND_TEXT,
        expect.any(Boolean),
      );
    });

    test("1단계: idle + inbound + grace 경과 → tmux로 remind 주입", () => {
      const { registry, tmux, observer, sessionId, tmuxName } = prep();
      const now = Date.now();
      registry.updateState(sessionId, {
        state: "idle",
        idleSinceTs: now - (IDLE_GRACE_MS + 5_000),
        inboundAt: now - 60_000,
      });
      (observer as any).tick();
      expect(tmux.sendKeys).toHaveBeenCalledWith(tmuxName, REMIND_TEXT, false);
      expect(tmux.sendKeys).toHaveBeenCalledWith(tmuxName, "Enter", false);
      const after = registry.get(sessionId)!;
      expect(after.remindSentAt).toBeDefined();
    });

    test("1단계: 같은 inbound 에 대해 중복 remind 안 함", () => {
      const { registry, tmux, observer, sessionId } = prep();
      const now = Date.now();
      registry.updateState(sessionId, {
        state: "idle",
        idleSinceTs: now - (IDLE_GRACE_MS + 5_000),
        inboundAt: now - 30_000,
        remindSentAt: now - 1_000, // already reminded for this inbound
      });
      (observer as any).tick();
      const remindCalls = (tmux.sendKeys as ReturnType<typeof mock>).mock.calls
        .filter((c) => c[1] === REMIND_TEXT);
      expect(remindCalls.length).toBe(0);
    });

    test("2단계: remind 후 STAGE2_DELAY_MS 경과 시 사용자 알림", () => {
      const { registry, announced, observer, sessionId } = prep();
      const now = Date.now();
      registry.updateState(sessionId, {
        state: "idle",
        idleSinceTs: now - (IDLE_GRACE_MS + STAGE2_DELAY_MS + 10_000),
        inboundAt: now - 60_000,
        remindSentAt: now - (STAGE2_DELAY_MS + 5_000),
      });
      (observer as any).tick();
      expect(announced.some((m) => m.includes("답변이 누락됐습니다"))).toBe(true);
      expect(announced.some((m) => m.includes(sessionId))).toBe(true);
      expect(registry.get(sessionId)!.userNoticeSentAt).toBeDefined();
    });

    test("2단계: 같은 inbound 에 대해 중복 알림 안 함", () => {
      const { registry, announced, observer, sessionId } = prep();
      const now = Date.now();
      registry.updateState(sessionId, {
        state: "idle",
        idleSinceTs: now - (IDLE_GRACE_MS + STAGE2_DELAY_MS + 10_000),
        inboundAt: now - 60_000,
        remindSentAt: now - (STAGE2_DELAY_MS + 5_000),
        userNoticeSentAt: now - 1_000, // already notified
      });
      (observer as any).tick();
      expect(announced.some((m) => m.includes("답변이 누락됐습니다"))).toBe(false);
    });

    test("새 inbound 도착 → busy→idle 전환 후 remind 사이클 리셋", () => {
      const { registry, tmux, observer, sessionId, tmuxName } = prep();
      const now = Date.now();
      registry.updateState(sessionId, {
        state: "idle",
        inboundAt: now - 40_000,        // inbound arrived 40s ago
        idleSinceTs: now - 20_000,      // idle transition AFTER inbound
        remindSentAt: now - 100_000,    // old remind, BEFORE inbound
      });
      (observer as any).tick();
      expect(tmux.sendKeys).toHaveBeenCalledWith(tmuxName, REMIND_TEXT, false);
      expect(tmux.sendKeys).toHaveBeenCalledWith(tmuxName, "Enter", false);
    });

    test("state=busy 면 발동 안 함", () => {
      const { registry, tmux, observer, sessionId } = prep();
      const now = Date.now();
      tmux.captured.set(registry.get(sessionId)!.tmuxName, "Running…\n> ");
      registry.updateState(sessionId, {
        state: "busy",
        idleSinceTs: now - (IDLE_GRACE_MS + 5_000),
        inboundAt: now - 30_000,
      });
      (observer as any).tick();
      expect(tmux.sendKeys).not.toHaveBeenCalledWith(
        expect.any(String),
        REMIND_TEXT,
        expect.any(Boolean),
      );
    });
  });
});
