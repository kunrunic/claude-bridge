import { describe, expect, test, mock } from "bun:test";
import { Registry } from "../src/core/registry.ts";
import { SlashHandler, type SlashHandlerDeps } from "../src/channels/telegram/SlashHandler.ts";
import type { TelegramClient } from "../src/channels/telegram/client.ts";
import type { SessionManager } from "../src/core/SessionManager.ts";
import type { SessionInfo } from "../src/core/sessions.ts";

// ── Fake implementations ──────────────────────────────────────────────────────

function fakeTelegramClient(): TelegramClient & {
  sendMessageCalls: Array<[string, string]>;
  sendWithKeyboardCalls: Array<[string, string]>;
  editWithKeyboardCalls: Array<[string, number, string]>;
} {
  const state: TelegramClient & {
    sendMessageCalls: Array<[string, string]>;
    sendWithKeyboardCalls: Array<[string, string]>;
    editWithKeyboardCalls: Array<[string, number, string]>;
  } = {
    token: "test_token",
    bot: {} as any,
    sendMessageCalls: [],
    sendWithKeyboardCalls: [],
    editWithKeyboardCalls: [],
    sendMessage: mock(async (chatId: string, text: string) => {
      state.sendMessageCalls.push([chatId, text]);
      return 1;
    }),
    sendWithKeyboard: mock(async (chatId: string, text: string, _kb: any) => {
      state.sendWithKeyboardCalls.push([chatId, text]);
      return 1;
    }),
    editWithKeyboard: mock(async (chatId: string, msgId: number, text: string) => {
      state.editWithKeyboardCalls.push([chatId, msgId, text]);
    }),
    sendFile: mock(async () => 1),
    editMessage: mock(async () => {}),
    setReaction: mock(async () => {}),
  } as any;
  return state;
}

function fakeSessionManager(): SessionManager & {
  spawnCalls: Array<any>;
  killCalls: string[];
  resumeCalls: Array<[string, boolean, boolean]>;
  listRecentCalls: number;
  refreshPickerCacheCalls: number;
} {
  const state: SessionManager & {
    spawnCalls: Array<any>;
    killCalls: string[];
    resumeCalls: Array<[string, boolean, boolean]>;
    listRecentCalls: number;
    refreshPickerCacheCalls: number;
  } = {
    spawnCalls: [],
    killCalls: [],
    resumeCalls: [],
    listRecentCalls: 0,
    refreshPickerCacheCalls: 0,
    spawn: mock((opts: any) => ({ id: "s1", label: "spawned" })),
    kill: mock((target: string) => {
      state.killCalls.push(target);
      return true;
    }),
    resume: mock((target: string, fork: boolean, skipPermissions = false) => {
      state.resumeCalls.push([target, fork, skipPermissions]);
      return `resumed ${target}`;
    }),
    listRecent: mock(() => {
      state.listRecentCalls++;
      return "recent list";
    }),
    refreshPickerCache: mock(() => {
      state.refreshPickerCacheCalls++;
      return [];
    }),
    gracefulKill: mock(async () => {}),
  } as any;
  return state;
}

// ── SlashHandler tests ────────────────────────────────────────────────────────

describe("SlashHandler", () => {
  describe("handle()", () => {
    test("{kind:'sessions'} no sessions → sendMessage 'no sessions'", async () => {
      const registry = new Registry();
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.handle({ kind: "sessions" }, "chat1");

      expect(tg.sendMessageCalls.length).toBe(1);
      expect(tg.sendMessageCalls[0]![1]).toMatch(/no sessions/);
    });

    test("{kind:'sessions'} 2 sessions → sendWithKeyboard with switch entries", async () => {
      const registry = new Registry();
      registry.create("alpha");
      registry.create("beta");
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.handle({ kind: "sessions" }, "chat1");

      expect(tg.sendWithKeyboardCalls.length).toBe(1);
      expect(tg.sendWithKeyboardCalls[0]![1]).toMatch(/세션 선택/);
    });

    test("{kind:'kill'} no target, no sessions → sendMessage 'no sessions to kill'", async () => {
      const registry = new Registry();
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.handle({ kind: "kill" }, "chat1");

      expect(tg.sendMessageCalls.length).toBe(1);
      expect(tg.sendMessageCalls[0]![1]).toMatch(/no sessions to kill/);
    });

    test("{kind:'kill'} no target, 1 session → sendWithKeyboard with kill entry", async () => {
      const registry = new Registry();
      registry.create("alpha");
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.handle({ kind: "kill" }, "chat1");

      expect(tg.sendWithKeyboardCalls.length).toBe(1);
      expect(tg.sendWithKeyboardCalls[0]![1]).toMatch(/종료할 세션/);
    });

    test("{kind:'resume'} no target, no cache → sendMessage 'no recent sessions'", async () => {
      const registry = new Registry();
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.handle({ kind: "resume" }, "chat1");

      expect(tg.sendMessageCalls.length).toBe(1);
      expect(tg.sendMessageCalls[0]![1]).toMatch(/no recent sessions/);
    });

    test("{kind:'resume'} no target, cache has entries → sendWithKeyboard with resume entries", async () => {
      const registry = new Registry();
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      // Mock refreshPickerCache to return sessions
      sessions.refreshPickerCache = mock(() => [
        {
          id: "sess1",
          project: "proj1",
          title: "test title",
          last: "last msg",
          mtime: Date.now(),
          mtimeText: "12/25 10:30",
        },
      ]);
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.handle({ kind: "resume" }, "chat1");

      expect(tg.sendWithKeyboardCalls.length).toBe(1);
      expect(tg.sendWithKeyboardCalls[0]![1]).toMatch(/복원 세션 선택/);
    });

    test("{kind:'fork'} no target, cache has entries → sendWithKeyboard with fork entries", async () => {
      const registry = new Registry();
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      sessions.refreshPickerCache = mock(() => [
        {
          id: "sess1",
          project: "proj1",
          title: "test title",
          last: "last msg",
          mtime: Date.now(),
          mtimeText: "12/25 10:30",
        },
      ]);
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.handle({ kind: "fork" }, "chat1");

      expect(tg.sendWithKeyboardCalls.length).toBe(1);
      expect(tg.sendWithKeyboardCalls[0]![1]).toMatch(/포크 세션 선택/);
    });

    test("{kind:'new'} → perm picker keyboard 표시 (spawn 미호출)", async () => {
      const registry = new Registry();
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.handle({ kind: "new" }, "chat1");

      expect(tg.sendWithKeyboardCalls.length).toBe(1);
      expect(tg.sendWithKeyboardCalls[0]![1]).toMatch(/권한 설정/);
      expect((sessions.spawn as ReturnType<typeof mock>).mock.calls.length).toBe(0);
    });

    test("{kind:'kill', target:'s1'} → sessions.kill called", async () => {
      const registry = new Registry();
      registry.create("alpha");
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.handle({ kind: "kill", target: "s1" }, "chat1");

      expect(sessions.killCalls.length).toBeGreaterThan(0);
    });
  });

  describe("onSessionAction()", () => {
    test("action='cancel' → editWithKeyboard with '✖ cancelled'", async () => {
      const registry = new Registry();
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.onSessionAction("cancel", "", "chat1", 123);

      expect(tg.editWithKeyboardCalls.length).toBe(1);
      expect(tg.editWithKeyboardCalls[0]![2]).toMatch(/✖ cancelled/);
    });

    test("action='kill', target='s1' → sessions.kill called, editWithKeyboard with result", async () => {
      const registry = new Registry();
      registry.create("alpha");
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.onSessionAction("kill", "s1", "chat1", 123);

      expect(sessions.killCalls.length).toBeGreaterThan(0);
      expect(tg.editWithKeyboardCalls.length).toBe(1);
    });

    test("action='switch', target='s1' → registry.setActive called, editWithKeyboard with switched message", async () => {
      const registry = new Registry();
      const s1 = registry.create("alpha");
      const s2 = registry.create("beta");
      registry.setActive(s1.id);
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.onSessionAction("switch", s2.id, "chat1", 123);

      expect(registry.active()!.id).toBe(s2.id);
      expect(tg.editWithKeyboardCalls.length).toBe(1);
      expect(tg.editWithKeyboardCalls[0]![2]).toMatch(/▶ switched/);
    });

    test("action='switch', target='ghost' → editWithKeyboard with 'no such session'", async () => {
      const registry = new Registry();
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.onSessionAction("switch", "ghost", "chat1", 123);

      expect(tg.editWithKeyboardCalls.length).toBe(1);
      expect(tg.editWithKeyboardCalls[0]![2]).toMatch(/no such session/);
    });

    test("action='resume' → perm picker keyboard 표시 (sessions.resume 미호출)", async () => {
      const registry = new Registry();
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.onSessionAction("resume", "sess-123", "chat1", 123);

      // perm picker 표시 (editWithKeyboard 호출), sessions.resume 미호출
      expect(tg.editWithKeyboardCalls.length).toBe(1);
      expect(tg.editWithKeyboardCalls[0]![2]).toMatch(/권한 설정 \(resume\)/);
      expect(sessions.resumeCalls.length).toBe(0);
    });

    test("action='fork' → perm picker keyboard 표시 (sessions.resume 미호출)", async () => {
      const registry = new Registry();
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.onSessionAction("fork", "sess-123", "chat1", 123);

      expect(tg.editWithKeyboardCalls.length).toBe(1);
      expect(tg.editWithKeyboardCalls[0]![2]).toMatch(/권한 설정 \(fork\)/);
      expect(sessions.resumeCalls.length).toBe(0);
    });

    test("action='resume_normal' → sessions.resume(id, false, false) 호출", async () => {
      const registry = new Registry();
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.onSessionAction("resume_normal", "sess-123", "chat1", 123);

      expect(sessions.resumeCalls.length).toBe(1);
      const [target, fork, skipPerm] = sessions.resumeCalls[0]!;
      expect(target).toBe("sess-123");
      expect(fork).toBe(false);
      expect(skipPerm).toBe(false);
    });

    test("action='fork_skip' → sessions.resume(id, true, true) 호출", async () => {
      const registry = new Registry();
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: mock(async () => {}),
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      await handler.onSessionAction("fork_skip", "sess-123", "chat1", 123);

      expect(sessions.resumeCalls.length).toBe(1);
      const [target, fork, skipPerm] = sessions.resumeCalls[0]!;
      expect(target).toBe("sess-123");
      expect(fork).toBe(true);
      expect(skipPerm).toBe(true);
    });

    test("pin updated when active session changes after handle", async () => {
      const registry = new Registry();
      const s1 = registry.create("alpha");
      const s2 = registry.create("beta");
      registry.setActive(s1.id);
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const updatePinMock = mock(async () => {});
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: updatePinMock,
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      // Switch active session
      await handler.onSessionAction("switch", s2.id, "chat1", 123);

      expect(updatePinMock).toHaveBeenCalled();
    });

    test("pin updated when active session changes after onSessionAction", async () => {
      const registry = new Registry();
      const s1 = registry.create("alpha");
      const s2 = registry.create("beta");
      registry.setActive(s1.id);
      const tg = fakeTelegramClient();
      const sessions = fakeSessionManager();
      const updatePinMock = mock(async () => {});
      const deps: SlashHandlerDeps = {
        registry,
        tg,
        sessions,
        doUpdateActivePin: updatePinMock,
        renderStatus: () => "status",
      };
      const handler = new SlashHandler(deps);

      // Switch active session via action
      await handler.onSessionAction("switch", s2.id, "chat1", 123);

      expect(updatePinMock).toHaveBeenCalled();
    });
  });
});
