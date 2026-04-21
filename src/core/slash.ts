export type SlashCommand =
  | { kind: "sessions" }
  | { kind: "new"; label?: string; cwd?: string }
  | { kind: "kill"; target?: string }
  | { kind: "backlog"; target?: string }
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
      const parts = arg.split(/\s+/);
      if (parts.length === 1) {
        const only = parts[0]!;
        return isPath(only)
          ? { kind: "new", cwd: only }
          : { kind: "new", label: only };
      }
      const label = parts[0]!;
      const cwd = parts.slice(1).join(" ");
      return { kind: "new", label, cwd };
    }
    case "kill":
      return arg ? { kind: "kill", target: arg } : { kind: "kill" };
    case "backlog":
      return arg ? { kind: "backlog", target: arg } : { kind: "backlog" };
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
  { command: "new", description: "새 세션 시작 — /new [label] [cwd]" },
  { command: "resume", description: "이전 세션 복원" },
  { command: "fork", description: "이전 세션 컨텍스트 이어 새 세션 시작" },
  { command: "kill", description: "세션 종료 — /kill <id|label>" },
];

export function help(): string {
  return [
    "Commands:",
    "  /sessions            활성 세션 목록 / 탭해서 전환",
    "  /new [label] [cwd]   새 세션 시작",
    "  /resume [N|id]       이전 Claude 세션 복원",
    "  /fork [N|id]         이전 세션 컨텍스트 상속 + 새 session-id",
    "  /kill [id|label]     세션 종료 (picker)",
    "  /backlog [id|label]  backlog 조회",
  ].join("\n");
}
