/**
 * 메인 화면 — 활성 세션 리스트 + 액션 패널.
 *
 * 키:
 *   ↑↓ / j k         선택
 *   Enter            switch-client → 해당 세션 attach (이 메뉴 client 가 점프)
 *   1~9              직접 인덱스 선택 + 즉시 switch
 *   n                new session 모드 (cwd browser)
 *   r                resume 모드 (list_recent picker)
 *   x                선택한 세션 kill (확인 표시)
 *   q                detach (메뉴 client 만 끊김 — 메뉴 자체는 영속)
 */

import { Box, Text, useInput } from "ink";
import { useEffect, useState } from "react";
import type { CliSessionInfo } from "../../../core/ipc.ts";
import { useSpinnerFrame } from "./spinner.ts";

type Props = {
  sessions: CliSessionInfo[];
  onSelect: (s: CliSessionInfo) => void;
  onNew: () => void;
  onResume: () => void;
  onKill: (s: CliSessionInfo) => void;
  onDetach: () => void;
};

function sigil(s: CliSessionInfo, spin: string): string {
  if (s.state === "dead") return "✗";
  if (s.state === "error") return "!";
  if (s.state === "spawning") return spin;
  switch (s.signal) {
    case "busy":
    case "trust_prompt":
    case "resume_picker":
      return spin;
    case "compact":
    case "compact_error":
    case "context_limit":
      return "□";
    case "rate_limit":
      return "⏸";
    default:
      return "·";
  }
}

function stateLabel(s: CliSessionInfo): string {
  if (s.state === "dead") return "dead";
  if (s.state === "error") return "error";
  if (s.state === "spawning") return "spawning";
  if (s.signal === "busy") return "busy";
  if (s.signal === "compact") return "compact";
  if (s.signal === "rate_limit") return "rate-limit";
  return "idle";
}

export function SessionList({ sessions, onSelect, onNew, onResume, onKill, onDetach }: Props) {
  const rows = sessions.filter((s) => s.state !== "dead");
  const [idx, setIdx] = useState(0);
  const [confirmKill, setConfirmKill] = useState<CliSessionInfo | undefined>();
  const spin = useSpinnerFrame();

  // 세션 목록 갱신될 때 idx 가 범위 밖으로 나가지 않게 클램프.
  useEffect(() => {
    if (idx >= rows.length) setIdx(Math.max(0, rows.length - 1));
  }, [rows.length, idx]);

  // 처음 렌더 시 active 세션을 기본 커서로.
  useEffect(() => {
    if (rows.length === 0) return;
    const i = rows.findIndex((s) => s.isActive);
    if (i >= 0) setIdx(i);
    // sessions 가 처음 채워질 때 한 번만. 이후 갱신은 사용자가 옮긴 idx 유지.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows.length > 0]);

  useInput((input, key) => {
    if (confirmKill) {
      if (input === "y" || input === "Y") {
        onKill(confirmKill);
        setConfirmKill(undefined);
      } else {
        setConfirmKill(undefined);
      }
      return;
    }
    if (key.upArrow || input === "k") {
      setIdx((i) => Math.max(0, i - 1));
    } else if (key.downArrow || input === "j") {
      setIdx((i) => Math.min(rows.length - 1, i + 1));
    } else if (key.return) {
      const s = rows[idx];
      if (s) onSelect(s);
    } else if (input === "n") {
      onNew();
    } else if (input === "r") {
      onResume();
    } else if (input === "x") {
      const s = rows[idx];
      if (s) setConfirmKill(s);
    } else if (input === "q") {
      onDetach();
    } else if (/^[1-9]$/.test(input)) {
      const n = Number(input) - 1;
      const s = rows[n];
      if (s) {
        setIdx(n);
        onSelect(s);
      }
    }
  });

  return (
    <Box flexDirection="column">
      <Text bold>Sessions</Text>
      {rows.length === 0 && (
        <Box marginTop={1}>
          <Text dimColor>활성 세션 없음 — </Text>
          <Text color="green">n</Text>
          <Text dimColor>=new </Text>
          <Text color="green">r</Text>
          <Text dimColor>=resume </Text>
          <Text color="green">q</Text>
          <Text dimColor>=detach</Text>
        </Box>
      )}
      {rows.length > 0 && (
        <Box flexDirection="column" marginTop={1}>
          {rows.map((s, i) => {
            const cursor = i === idx ? "▶" : " ";
            const activeTag = s.isActive ? " (active)" : "";
            const colorProps = i === idx ? { color: "cyan" as const } : {};
            return (
              <Text key={s.id} {...colorProps}>
                {cursor} {String(i + 1)}. {s.id} {sigil(s, spin)} {s.label.padEnd(14).slice(0, 14)} {stateLabel(s)}{activeTag}
              </Text>
            );
          })}
        </Box>
      )}
      <Box marginTop={1}>
        {confirmKill ? (
          <Text color="yellow">
            kill [{confirmKill.id}] {confirmKill.label}? (y/n)
          </Text>
        ) : (
          <Text dimColor>
            ↑↓ 이동 · Enter attach · n new · r resume · x kill · q detach
          </Text>
        )}
      </Box>
    </Box>
  );
}
