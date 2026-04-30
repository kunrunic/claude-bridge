/**
 * 미니맵 렌더링 — registry 의 세션 목록을 한 줄짜리 status bar 텍스트로 변환.
 *
 * Pure function. 테스트는 tests/tmux-render.test.ts 참조.
 *
 * 형식: ▶s1·my-app  s2⚠backend  s3⠋docs  +N
 *  - 첫 글자: 자기 자신이면 ▶, 아니면 공백
 *  - sid (s1, s2, ...)
 *  - signal/state sigil (·, ⠋, ⚠ 등)
 *  - label (작업 폴더) — 8자 초과 시 7자 + …
 *  - 6 개 초과 시 +N 으로 잔여 카운트 표시
 */

import type { Session } from "../../core/registry.ts";

const MAX_VISIBLE = 6;
const MAX_LABEL_LEN = 8;

export const SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"] as const;

const SIGIL = {
  perm: "⚠",
  compact: "□",
  rate_limit: "⏸",
  dead: "✗",
  error: "!",
  idle: "·",
} as const;

function sigilOf(s: Session, spin: string): string {
  if (s.state === "dead") return SIGIL.dead;
  if (s.state === "error") return SIGIL.error;
  if (s.state === "spawning") return spin;
  if (s.pendingPermissions.size > 0) return SIGIL.perm;
  switch (s.signal) {
    case "busy":
    case "trust_prompt":
    case "resume_picker":
      return spin;
    case "compact":
    case "compact_error":
    case "context_limit":
      return SIGIL.compact;
    case "rate_limit":
      return SIGIL.rate_limit;
    default:
      return SIGIL.idle;
  }
}

function truncateLabel(label: string): string {
  if (label.length <= MAX_LABEL_LEN) return label;
  return label.slice(0, MAX_LABEL_LEN - 1) + "…";
}

/**
 * 주어진 세션 목록을 status-right 한 줄로 렌더링.
 * `viewerSessionId` 와 일치하는 세션은 ▶ 로 표시 (각 tmux 세션의 status bar 가
 * 자기 자신을 ▶ 로 보여줌으로써 "여기가 어디인지" 표시).
 *
 * `spin` 은 SPINNER_FRAMES 중 하나. busy / spawning 등 spinner 의미가 있는 sigil
 * 자리에 그대로 박힌다. 호출자(TmuxStatusChannel) 가 frame index 관리.
 */
export function renderMinimap(
  sessions: readonly Session[],
  viewerSessionId: string | undefined,
  spin: string = SPINNER_FRAMES[0],
): string {
  // dead 세션은 minimap 에 안 보임 — cb-menu 의 SessionList 와 일관 유지.
  // registry 에서 dead 가 즉시 제거되지 않는 흐름(예: Claude /exit 으로 MCP
  // socket 만 끊기는 경우)이 있어 view-side 에서 필터링.
  const alive = sessions.filter((s) => s.state !== "dead");
  const sorted = [...alive].sort((a, b) => a.id.localeCompare(b.id));
  const visible = sorted.slice(0, MAX_VISIBLE);
  const overflow = sorted.length - visible.length;
  const parts = visible.map((s) => {
    const prefix = s.id === viewerSessionId ? "▶" : " ";
    return `${prefix}${s.id}${sigilOf(s, spin)}${truncateLabel(s.label)}`;
  });
  if (overflow > 0) parts.push(`+${overflow}`);
  return parts.join("  ");
}

/** busy / spawning / trust_prompt / resume_picker 가 하나라도 있으면 true. */
export function hasAnimatedSignal(sessions: readonly Session[]): boolean {
  return sessions.some((s) => {
    if (s.state === "spawning") return true;
    return (
      s.signal === "busy" ||
      s.signal === "trust_prompt" ||
      s.signal === "resume_picker"
    );
  });
}
