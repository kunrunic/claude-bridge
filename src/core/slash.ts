export type SlashCommand =
  | { kind: "sessions" }
  | { kind: "new"; cwd?: string }
  | { kind: "kill"; target?: string }
  | { kind: "resume"; target?: string }
  | { kind: "fork"; target?: string };

const CMD_RE = /^\/([a-z]+)(?:\s+(.+))?$/;

function isPath(s: string): boolean {
  return s.startsWith("/") || s.startsWith("~");
}

export function parse(text: string): SlashCommand | undefined {
  const m = CMD_RE.exec(text.trim());
  if (!m) return undefined;
  const name = m[1]!;
  const arg = m[2]?.trim();
  switch (name) {
    case "sessions":
      return { kind: "sessions" };
    case "new": {
      if (!arg) return { kind: "new" };
      return { kind: "new", cwd: arg.trim() };
    }
    case "kill":
      return arg ? { kind: "kill", target: arg } : { kind: "kill" };
    case "resume":
      return arg ? { kind: "resume", target: arg } : { kind: "resume" };
    case "fork":
      return arg ? { kind: "fork", target: arg } : { kind: "fork" };
    default:
      return undefined;
  }
}

export const BOT_COMMANDS: Array<{ command: string; description: string }> = [
  { command: "sessions", description: "세션 목록 / 전환" },
  { command: "new", description: "새 세션 시작 — /new [cwd]" },
  { command: "resume", description: "이전 세션 복원" },
  { command: "fork", description: "이전 세션 컨텍스트 이어 새 세션 시작" },
  { command: "kill", description: "세션 종료 — /kill <id|label>" },
];

export function help(): string {
  return [
    "Commands:",
    "  /sessions            활성 세션 목록 / 탭해서 전환",
    "  /new [cwd]           새 세션 시작 (label은 폴더명에서 자동 추출)",
    "  /resume [N|id]       이전 Claude 세션 복원",
    "  /fork [N|id]         이전 세션 컨텍스트 상속 + 새 session-id",
    "  /kill [id|label]     세션 종료 (picker)",
  ].join("\n");
}
