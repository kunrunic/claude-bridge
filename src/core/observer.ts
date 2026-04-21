export const COMPACT_RE = /Compacting\.{0,3}|Compaction complete/i;
export const COMPACT_ERROR_RE = /compact(?:ion)?\s+(?:failed|error)/i;
export const CONTEXT_LIMIT_RE = /context\s+(?:limit|window)\s+(?:reached|exceeded|full)/i;
export const TRUST_PROMPT_RE = /Trust this folder\?|Do you trust this folder/i;
export const RESUME_PICKER_RE = /Resume:\s+\?.*which conversation|^\s*\d+\.\s+\S+/m;
export const BUSY_ACTIVE_RE = /\(\d+s\s*·\s*[↑↓]|Running…|Waiting…|ctrl\+b.*background|esc\s+to\s+interrupt/i;
export const INPUT_READY_RE = /^\s*[❯>]\s*$/m;
export const RATE_LIMIT_RE = /You've hit your limit/i;
export const RATE_LIMIT_RESET_RE = /resets\s+(\S+(?:\s+\(\S+\))?)/i;

export type Signal =
  | "idle"
  | "busy"
  | "compact"
  | "compact_error"
  | "context_limit"
  | "trust_prompt"
  | "resume_picker"
  | "rate_limit";

export type Observation = {
  signal: Signal;
  lastLines: string;
  ts: number;
};

export function observe(paneText: string): Observation {
  const tail = paneText.split("\n").slice(-20).join("\n");
  const ts = Date.now();
  if (COMPACT_ERROR_RE.test(tail)) return { signal: "compact_error", lastLines: tail, ts };
  if (COMPACT_RE.test(tail)) return { signal: "compact", lastLines: tail, ts };
  if (CONTEXT_LIMIT_RE.test(tail)) return { signal: "context_limit", lastLines: tail, ts };
  if (RATE_LIMIT_RE.test(tail)) return { signal: "rate_limit", lastLines: tail, ts };
  if (TRUST_PROMPT_RE.test(tail)) return { signal: "trust_prompt", lastLines: tail, ts };
  if (RESUME_PICKER_RE.test(tail)) return { signal: "resume_picker", lastLines: tail, ts };
  if (BUSY_ACTIVE_RE.test(tail)) return { signal: "busy", lastLines: tail, ts };
  if (INPUT_READY_RE.test(tail)) return { signal: "idle", lastLines: tail, ts };
  return { signal: "busy", lastLines: tail, ts };
}
