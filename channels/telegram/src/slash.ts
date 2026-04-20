export type SlashCommand =
  | { kind: "sessions" }
  | { kind: "new"; label?: string; cwd?: string }
  | { kind: "switch"; target: string }
  | { kind: "kill"; target: string }
  | { kind: "current" }
  | { kind: "backlog"; target?: string }
  | { kind: "resume"; target?: string }
  | { kind: "fork"; target?: string }
  | { kind: "status" };

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
    case "switch":
      if (!arg) return undefined;
      return { kind: "switch", target: arg };
    case "kill":
      if (!arg) return undefined;
      return { kind: "kill", target: arg };
    case "current":
      return { kind: "current" };
    case "backlog":
      return arg ? { kind: "backlog", target: arg } : { kind: "backlog" };
    case "resume":
      return arg ? { kind: "resume", target: arg } : { kind: "resume" };
    case "fork":
      return arg ? { kind: "fork", target: arg } : { kind: "fork" };
    case "status":
      return { kind: "status" };
    default:
      return undefined;
  }
}

export function help(): string {
  return [
    "Commands:",
    "  /sessions            list all sessions",
    "  /new [label] [cwd]   spawn a new Claude session (optional cwd)",
    "  /resume [N|id]       list recent Claude sessions, or resume one (same session-id)",
    "  /fork [N|id]         like /resume but starts a new session-id (inherits context)",
    "  /switch <id|label>   make a session active",
    "  /kill <id|label>     terminate a session",
    "  /current             show the active session",
    "  /backlog [id|label]  show backlog",
    "  /status              recent anomaly count (24h)",
  ].join("\n");
}
