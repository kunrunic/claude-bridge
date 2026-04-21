import { describe, expect, test } from "bun:test";
import { Registry } from "../src/core/registry.ts";
import * as slash from "../src/core/slash.ts";
import { observe } from "../src/core/observer.ts";
import { CALLBACK_RE, REPLY_RE } from "../src/channels/telegram/permissions.ts";
import { CONFLICT_RE, SESSION_CB_RE } from "../src/channels/telegram/poller.ts";
import { assertSendable, chunkByNewline } from "../src/channels/telegram/server.ts";
import { homedir } from "node:os";
import { join } from "node:path";

describe("registry", () => {
  test("create + active pointer", () => {
    const r = new Registry();
    const s1 = r.create("alpha");
    const s2 = r.create();
    expect(s1.id).toBe("s1");
    expect(s2.id).toBe("s2");
    expect(r.active()?.id).toBe("s1");
    r.setActive("s2");
    expect(r.active()?.id).toBe("s2");
  });

  test("resetSeq restarts id from s1", () => {
    const r = new Registry();
    r.create("alpha");
    r.create("beta");
    r.remove("s1");
    r.remove("s2");
    r.resetSeq();
    const s = r.create("gamma");
    expect(s.id).toBe("s1");
  });

  test("setTmuxPrefix uses prefix for new sessions", () => {
    const r = new Registry();
    r.setTmuxPrefix("cb-work-");
    const s = r.create("alpha");
    expect(s.tmuxName).toBe("cb-work-s1");
  });

  test("default tmux prefix is cb-", () => {
    const r = new Registry();
    const s = r.create("alpha");
    expect(s.tmuxName).toBe("cb-s1");
  });

  test("lookup by id or label", () => {
    const r = new Registry();
    r.create("alpha");
    expect(r.getByLabel("alpha")?.id).toBe("s1");
    expect(r.get("s1")?.label).toBe("alpha");
  });

  test("remove reassigns active", () => {
    const r = new Registry();
    r.create();
    r.create();
    r.remove("s1");
    expect(r.active()?.id).toBe("s2");
  });

});

describe("slash parser", () => {
  test("/sessions", () => {
    expect(slash.parse("/sessions")).toEqual({ kind: "sessions" });
  });
  test("/new no arg → cwd 없음", () => {
    expect(slash.parse("/new")).toEqual({ kind: "new" });
  });
  test("/new /tmp/x → cwd=/tmp/x", () => {
    expect(slash.parse("/new /tmp/x")).toEqual({ kind: "new", cwd: "/tmp/x" });
  });
  test("/new ~/cb_test → cwd=~/cb_test", () => {
    expect(slash.parse("/new ~/cb_test")).toEqual({ kind: "new", cwd: "~/cb_test" });
  });
  test("/new arg with spaces → cwd 로 처리", () => {
    expect(slash.parse("/new /tmp/my dir")).toEqual({ kind: "new", cwd: "/tmp/my dir" });
  });
  test("/resume no arg", () => {
    expect(slash.parse("/resume")).toEqual({ kind: "resume" });
  });
  test("/resume with index", () => {
    expect(slash.parse("/resume 3")).toEqual({ kind: "resume", target: "3" });
  });
  test("/fork no arg", () => {
    expect(slash.parse("/fork")).toEqual({ kind: "fork" });
  });
  test("/fork with index", () => {
    expect(slash.parse("/fork 2")).toEqual({ kind: "fork", target: "2" });
  });
  test("/kill no arg → picker intent", () => {
    expect(slash.parse("/kill")).toEqual({ kind: "kill" });
  });
  test("/kill with target", () => {
    expect(slash.parse("/kill s1")).toEqual({ kind: "kill", target: "s1" });
  });
  test("unknown command", () => {
    expect(slash.parse("/nope")).toBeUndefined();
  });
  test("non-slash text", () => {
    expect(slash.parse("hello world")).toBeUndefined();
  });
});

describe("observer", () => {
  test("busy detected", () => {
    expect(observe("⏺ Running…\nesc to interrupt").signal).toBe("busy");
  });
  test("busy detected (Claude v2.1.x token counter)", () => {
    const pane = "✻ Nebulizing… (2s · ↑ 77 tokens)\n────\n❯ \n────";
    expect(observe(pane).signal).toBe("busy");
  });
  test("busy detected (various verbs)", () => {
    for (const verb of ["Brewing", "Churning", "Cogitating", "Sautéing"]) {
      const pane = `✻ ${verb}… (15s · ↓ 342 tokens)\n❯ `;
      expect(observe(pane).signal).toBe("busy");
    }
  });
  test("idle prompt", () => {
    expect(observe("last message\n❯ ").signal).toBe("idle");
  });
  test("compact", () => {
    expect(observe("Compacting...\nplease wait").signal).toBe("compact");
  });
  test("context limit", () => {
    expect(observe("Context limit reached, trim required").signal).toBe("context_limit");
  });
  test("rate limit dialog detected", () => {
    const pane = "You've hit your limit · resets 3am (Asia/Seoul)\n❯ 1. Stop and wait";
    expect(observe(pane).signal).toBe("rate_limit");
  });
});

describe("session action callbacks", () => {
  test("kill callback parsed", () => {
    const m = SESSION_CB_RE.exec("kill:s12");
    expect(m?.[1]).toBe("kill");
    expect(m?.[2]).toBe("s12");
  });
  test("resume callback parsed", () => {
    const m = SESSION_CB_RE.exec("resume:abc-def-123");
    expect(m?.[1]).toBe("resume");
    expect(m?.[2]).toBe("abc-def-123");
  });
  test("fork callback parsed", () => {
    const m = SESSION_CB_RE.exec("fork:abc-123");
    expect(m?.[1]).toBe("fork");
    expect(m?.[2]).toBe("abc-123");
  });
  test("switch callback parsed", () => {
    const m = SESSION_CB_RE.exec("switch:s3");
    expect(m?.[1]).toBe("switch");
    expect(m?.[2]).toBe("s3");
  });
  test("cancel callback parsed (empty target)", () => {
    const m = SESSION_CB_RE.exec("cancel:");
    expect(m?.[1]).toBe("cancel");
    expect(m?.[2]).toBe("");
  });
  test("permission callback not matched", () => {
    expect(SESSION_CB_RE.exec("perm:allow:abcde")).toBeNull();
  });
});

describe("permission regexes", () => {
  test("callback format", () => {
    const m = CALLBACK_RE.exec("perm:allow:abcde");
    expect(m?.[1]).toBe("allow");
    expect(m?.[2]).toBe("abcde");
  });
  test("text reply yes", () => {
    const m = REPLY_RE.exec("yes abcde");
    expect(m?.[1]?.toLowerCase()).toBe("yes");
    expect(m?.[2]).toBe("abcde");
  });
  test("text reply n", () => {
    const m = REPLY_RE.exec("n abcde");
    expect(m?.[1]?.toLowerCase()).toBe("n");
  });
  test("invalid callback ignored", () => {
    expect(CALLBACK_RE.exec("perm:huh:abcde")).toBeNull();
    expect(CALLBACK_RE.exec("perm:allow:abcl0")).toBeNull();
  });
});

describe("chunkByNewline", () => {
  test("short text → 1 chunk", () => {
    expect(chunkByNewline("hello", 100)).toEqual(["hello"]);
  });
  test("splits on newline boundary", () => {
    const s = "line1\nline2\nline3";
    const out = chunkByNewline(s, 12);
    expect(out).toEqual(["line1\nline2\n", "line3"]);
  });
  test("single line longer than limit → hard split", () => {
    const out = chunkByNewline("aaaaaaaaaa", 4);
    expect(out).toEqual(["aaaa", "aaaa", "aa"]);
  });
  test("preserves paragraph boundaries over absolute cut", () => {
    const s = "aaa\n" + "b".repeat(10) + "\nccc";
    const out = chunkByNewline(s, 12);
    expect(out[0]).toBe("aaa\n");
    expect(out[1]).toBe("b".repeat(10) + "\n");
  });
});

describe("assertSendable", () => {
  test("allows normal paths", () => {
    expect(() => assertSendable("/tmp/report.png")).not.toThrow();
  });
  test("blocks ~/.claude-bridge contents", () => {
    expect(() =>
      assertSendable(join(homedir(), ".claude-bridge", "config.json")),
    ).toThrow(/protected/);
  });
  test("blocks ~/.claude/channels contents", () => {
    expect(() =>
      assertSendable(join(homedir(), ".claude", "channels", "telegram", ".env")),
    ).toThrow(/protected/);
  });
  test("resolves .. traversal", () => {
    expect(() =>
      assertSendable(join(homedir(), ".claude-bridge", "..", ".claude-bridge", "x")),
    ).toThrow(/protected/);
  });
});

describe("token collision detection", () => {
  test("matches real grammy 409 error text", () => {
    const real =
      "GrammyError: Call to 'getUpdates' failed! " +
      "(409: Conflict: terminated by other getUpdates request; " +
      "make sure that only one bot instance is running)";
    expect(CONFLICT_RE.test(real)).toBe(true);
  });
  test("ignores other errors", () => {
    expect(CONFLICT_RE.test("NetworkError: httpx.ConnectError")).toBe(false);
    expect(CONFLICT_RE.test("401: Unauthorized")).toBe(false);
  });
});
