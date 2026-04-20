import { describe, expect, test } from "bun:test";
import { Registry } from "../src/registry.ts";
import * as slash from "../src/slash.ts";
import { observe } from "../src/observer.ts";
import { CALLBACK_RE, REPLY_RE } from "../src/permissions.ts";
import { CONFLICT_RE } from "../src/telegram/poller.ts";
import { assertSendable, chunkByNewline } from "../src/server.ts";
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

  test("backlog cap", () => {
    const r = new Registry();
    r.create();
    for (let i = 0; i < 120; i++) r.pushBacklog("s1", `msg${i}`);
    const s = r.get("s1")!;
    expect(s.backlog.length).toBe(100);
    expect(s.backlog[0]).toBe("msg20");
  });
});

describe("slash parser", () => {
  test("/sessions", () => {
    expect(slash.parse("/sessions")).toEqual({ kind: "sessions" });
  });
  test("/new with label", () => {
    expect(slash.parse("/new backend")).toEqual({ kind: "new", label: "backend" });
  });
  test("/new no label", () => {
    expect(slash.parse("/new")).toEqual({ kind: "new" });
  });
  test("/new label + cwd", () => {
    expect(slash.parse("/new backend /tmp/x")).toEqual({
      kind: "new",
      label: "backend",
      cwd: "/tmp/x",
    });
  });
  test("/new single arg starting with / → cwd only", () => {
    expect(slash.parse("/new /tmp/x")).toEqual({ kind: "new", cwd: "/tmp/x" });
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
  test("/switch requires arg", () => {
    expect(slash.parse("/switch")).toBeUndefined();
    expect(slash.parse("/switch s2")).toEqual({ kind: "switch", target: "s2" });
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
  test("idle prompt", () => {
    expect(observe("last message\n❯ ").signal).toBe("idle");
  });
  test("compact", () => {
    expect(observe("Compacting...\nplease wait").signal).toBe("compact");
  });
  test("context limit", () => {
    expect(observe("Context limit reached, trim required").signal).toBe("context_limit");
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
