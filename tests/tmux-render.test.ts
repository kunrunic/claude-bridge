import { describe, expect, test } from "bun:test";
import type { Session } from "../src/core/registry.ts";
import { renderMinimap } from "../src/channels/tmux/render.ts";

function makeSession(overrides: Partial<Session> & { id: string; label: string }): Session {
  return {
    state: "idle",
    signal: "idle",
    tmuxName: `cb-${overrides.id}`,
    pendingPermissions: new Set<string>(),
    ...overrides,
  };
}

describe("renderMinimap", () => {
  test("빈 목록 → 빈 문자열", () => {
    expect(renderMinimap([], undefined)).toBe("");
  });

  test("단일 세션, viewer 일치 → ▶ prefix + label", () => {
    const s1 = makeSession({ id: "s1", label: "alpha" });
    expect(renderMinimap([s1], "s1")).toBe("▶s1·alpha");
  });

  test("단일 세션, viewer 미일치 → 공백 prefix + label", () => {
    const s1 = makeSession({ id: "s1", label: "alpha" });
    expect(renderMinimap([s1], "s2")).toBe(" s1·alpha");
  });

  test("label 8자 초과 → 7자 + …", () => {
    const s1 = makeSession({ id: "s1", label: "claude-bridge" }); // 13자
    expect(renderMinimap([s1], "s1")).toBe("▶s1·claude-…");
  });

  test("label 정확히 8자 → truncate 없음", () => {
    const s1 = makeSession({ id: "s1", label: "abcdefgh" });
    expect(renderMinimap([s1], "s1")).toBe("▶s1·abcdefgh");
  });

  test("idle 세션 → · sigil", () => {
    const s = makeSession({ id: "s1", label: "x", signal: "idle" });
    expect(renderMinimap([s], "s1")).toContain("·");
  });

  test("busy 세션 → ⠋ sigil", () => {
    const s = makeSession({ id: "s1", label: "x", signal: "busy" });
    expect(renderMinimap([s], "s1")).toContain("⠋");
  });

  test("compact → □ sigil", () => {
    const s = makeSession({ id: "s1", label: "x", signal: "compact" });
    expect(renderMinimap([s], "s1")).toContain("□");
  });

  test("rate_limit → ⏸ sigil", () => {
    const s = makeSession({ id: "s1", label: "x", signal: "rate_limit" });
    expect(renderMinimap([s], "s1")).toContain("⏸");
  });

  test("pendingPermissions 가 있으면 signal 무관하게 ⚠ sigil (perm 우선)", () => {
    const s = makeSession({
      id: "s1",
      label: "x",
      signal: "idle",
      pendingPermissions: new Set(["req-1"]),
    });
    expect(renderMinimap([s], "s1")).toContain("⚠");
  });

  test("dead 세션은 minimap 에 표시되지 않음 (filtered out)", () => {
    const dead = makeSession({ id: "s1", label: "x", state: "dead" });
    expect(renderMinimap([dead], "s1")).toBe("");
  });

  test("dead 와 alive 가 섞이면 alive 만 표시", () => {
    const dead = makeSession({ id: "s1", label: "old", state: "dead" });
    const alive = makeSession({ id: "s2", label: "new" });
    const out = renderMinimap([dead, alive], "s2");
    expect(out).not.toContain("s1");
    expect(out).toContain("s2");
  });

  test("spawning → spinner sigil (default frame ⠋)", () => {
    const s = makeSession({ id: "s1", label: "x", state: "spawning" });
    expect(renderMinimap([s], "s1")).toContain("⠋");
  });

  test("spawning → 호출자가 frame 지정", () => {
    const s = makeSession({ id: "s1", label: "x", state: "spawning" });
    expect(renderMinimap([s], "s1", "⠹")).toContain("⠹");
  });

  test("error → ! sigil", () => {
    const s = makeSession({ id: "s1", label: "x", state: "error" });
    expect(renderMinimap([s], "s1")).toContain("!");
  });

  test("여러 세션, id 순 정렬", () => {
    const s2 = makeSession({ id: "s2", label: "b" });
    const s1 = makeSession({ id: "s1", label: "a" });
    const s3 = makeSession({ id: "s3", label: "c" });
    const out = renderMinimap([s2, s1, s3], "s2");
    // s1 이 먼저, s2 가 ▶, s3 가 마지막
    const idx1 = out.indexOf("s1");
    const idx2 = out.indexOf("s2");
    const idx3 = out.indexOf("s3");
    expect(idx1).toBeLessThan(idx2);
    expect(idx2).toBeLessThan(idx3);
    expect(out).toContain("▶s2");
  });

  test("뷰어가 일치하는 세션 하나만 ▶, 나머지는 공백", () => {
    const sessions = [
      makeSession({ id: "s1", label: "a" }),
      makeSession({ id: "s2", label: "b" }),
      makeSession({ id: "s3", label: "c" }),
    ];
    const out = renderMinimap(sessions, "s2");
    // ▶ 는 한 번만 나와야 함
    const arrowCount = (out.match(/▶/g) ?? []).length;
    expect(arrowCount).toBe(1);
  });

  test("6개 초과 시 +N overflow 표시", () => {
    const sessions = Array.from({ length: 8 }, (_, i) =>
      makeSession({ id: `s${i + 1}`, label: `x${i + 1}` }),
    );
    const out = renderMinimap(sessions, "s1");
    expect(out).toContain("+2");
    // 처음 6개만 표시
    expect(out).toContain("s1");
    expect(out).toContain("s6");
    expect(out).not.toContain(" s7"); // 공백 prefix 형태로 들어가면 안 됨
  });

  test("정확히 6개 → overflow 없음", () => {
    const sessions = Array.from({ length: 6 }, (_, i) =>
      makeSession({ id: `s${i + 1}`, label: `x${i + 1}` }),
    );
    const out = renderMinimap(sessions, "s1");
    expect(out).not.toContain("+");
  });
});
