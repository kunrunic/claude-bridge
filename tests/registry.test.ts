import { describe, expect, test } from "bun:test";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Registry } from "../src/core/registry.ts";

function tmpFile(): string {
  const dir = mkdtempSync(join(tmpdir(), "cb-registry-"));
  return join(dir, "registry.json");
}

describe("Registry snapshot round-trip", () => {
  test("snapshot → loadSnapshot preserves seq / sessions / active", () => {
    const r1 = new Registry();
    const a = r1.create("alpha");
    const b = r1.create("bravo");
    r1.setActive(b.id);
    r1.pushBacklog(a.id, "hello");
    r1.updateState(a.id, { state: "idle", signal: "idle" });

    const snap = r1.snapshot();
    const r2 = new Registry();
    r2.loadSnapshot(snap);

    expect(r2.list().length).toBe(2);
    expect(r2.active()?.label).toBe("bravo");
    expect(r2.get(a.id)?.backlog).toEqual(["hello"]);
    const next = r2.create("charlie");
    expect(next.id).toBe("s3");
  });

  test("loadFrom is a no-op when file missing", () => {
    const r = new Registry();
    const path = join(tmpdir(), "cb-registry-missing", "no-such.json");
    expect(() => r.loadFrom(path)).not.toThrow();
    expect(r.list().length).toBe(0);
  });

  test("setPersistPath writes snapshot on mutations", () => {
    const path = tmpFile();
    const r = new Registry();
    r.setPersistPath(path);
    r.create("alpha");
    r.create("bravo");
    const raw = readFileSync(path, "utf-8");
    const snap = JSON.parse(raw);
    expect(snap.version).toBe(1);
    expect(snap.seq).toBe(2);
    expect(snap.sessions.length).toBe(2);
    expect(snap.sessions[0].label).toBe("alpha");
  });

  test("loadFrom reads persisted snapshot written by setPersistPath", () => {
    const path = tmpFile();
    const r1 = new Registry();
    r1.setPersistPath(path);
    r1.create("alpha");
    r1.create("bravo");

    const r2 = new Registry();
    r2.loadFrom(path);
    expect(r2.list().map((s) => s.label)).toEqual(["alpha", "bravo"]);
    const next = r2.create("charlie");
    expect(next.id).toBe("s3");
  });

  test("malformed persisted file is logged and ignored", () => {
    const path = tmpFile();
    writeFileSync(path, "{{{ not json", "utf-8");
    const r = new Registry();
    expect(() => r.loadFrom(path)).not.toThrow();
    expect(r.list().length).toBe(0);
  });

  test("mismatched schema version is ignored", () => {
    const path = tmpFile();
    writeFileSync(
      path,
      JSON.stringify({ version: 99, seq: 5, sessions: [] }),
      "utf-8",
    );
    const r = new Registry();
    r.loadFrom(path);
    expect(r.list().length).toBe(0);
    const first = r.create("alpha");
    expect(first.id).toBe("s1");
  });

  test("remove clears activeId when removing the active session", () => {
    const r = new Registry();
    const a = r.create("alpha");
    const b = r.create("bravo");
    r.setActive(a.id);
    r.remove(a.id);
    expect(r.active()?.id).toBe(b.id);
    r.remove(b.id);
    expect(r.active()).toBeUndefined();
  });

  test("activePin survives snapshot round-trip and persists on set", () => {
    const path = tmpFile();
    const r = new Registry();
    r.setPersistPath(path);
    r.create("alpha");
    r.setActivePin({ chatId: "123", messageId: 4567 });
    expect(r.getActivePin()).toEqual({ chatId: "123", messageId: 4567 });

    const r2 = new Registry();
    r2.loadFrom(path);
    expect(r2.getActivePin()).toEqual({ chatId: "123", messageId: 4567 });

    // clearing pin from a persistent instance should wipe from disk too
    r.setActivePin(undefined);
    const r3 = new Registry();
    r3.loadFrom(path);
    expect(r3.getActivePin()).toBeUndefined();
  });
});
