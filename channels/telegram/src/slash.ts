export type SlashCommand =
  | { kind: "sessions" }
  | { kind: "new"; label?: string }
  | { kind: "switch"; target: string }
  | { kind: "kill"; target: string }
  | { kind: "current" }
  | { kind: "backlog"; target?: string }
  | { kind: "status" };

const CMD_RE = /^\/([a-z]+)(?:\s+(.+))?$/;

export function parse(text: string): SlashCommand | undefined {
  const m = CMD_RE.exec(text.trim());
  if (!m) return undefined;
  const name = m[1]!;
  const arg = m[2]?.trim();
  switch (name) {
    case "sessions":
      return { kind: "sessions" };
    case "new":
      return arg ? { kind: "new", label: arg } : { kind: "new" };
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
    "  /new [label]         spawn a new Claude session",
    "  /switch <id|label>   make a session active",
    "  /kill <id|label>     terminate a session",
    "  /current             show the active session",
    "  /backlog [id|label]  show backlog",
    "  /status              recent anomaly count (24h)",
  ].join("\n");
}
