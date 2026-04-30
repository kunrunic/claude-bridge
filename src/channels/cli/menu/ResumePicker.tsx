/**
 * Resume picker — list_recent 결과를 보여주고 ID 선택 → resume RPC.
 *
 * Type-ahead UX: 글자 입력하면 query 에 누적 → project / title / id 에 substring
 * 매치 필터링 (DirBrowser 와 동일 패턴).
 *
 * 키:
 *   (글자 입력)        query 에 append → 필터
 *   Backspace          query 한 글자 삭제
 *   ↑ ↓                항목 선택
 *   Enter              resume (해당 id 로)
 *   Ctrl-R             목록 새로고침
 *   Esc                query 가 있으면 query clear, 없으면 cancel
 */

import { Box, Text, useInput, useStdout } from "ink";
import { useState, useEffect, useMemo } from "react";
import type { CliRecentInfo } from "../../../core/ipc.ts";
import { rpc } from "./rpc.ts";

type Props = {
  onSelect: (id: string) => void;
  onCancel: () => void;
};

// 헤더/검색바/overflow/안내 등 고정 영역 = 약 10 줄. 단말 높이에서 빼고 본문 할당.
const RESERVED_ROWS = 10;
const MIN_VISIBLE = 5;

export function ResumePicker({ onSelect, onCancel }: Props) {
  const [items, setItems] = useState<CliRecentInfo[]>([]);
  const [idx, setIdx] = useState(0);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const { stdout } = useStdout();
  const termRows = stdout?.rows ?? 24;
  const visibleRows = Math.max(MIN_VISIBLE, termRows - RESERVED_ROWS);

  const refresh = (): void => {
    setLoading(true);
    // 화면이 넓으니 충분히 가져옴. 사용자는 type-ahead 로 좁힘.
    rpc({ op: "cli_request", command: "list_recent", limit: 64 })
      .then((r) => {
        if (r.ok && r.data?.kind === "list_recent") {
          setItems(r.data.recent);
          setError(undefined);
        } else {
          setError(r.error ?? "list_recent failed");
        }
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter(
      (i) =>
        i.project.toLowerCase().includes(q) ||
        i.title.toLowerCase().includes(q) ||
        i.id.toLowerCase().includes(q),
    );
  }, [items, query]);

  // 필터 또는 목록이 바뀔 때 idx 클램프.
  useEffect(() => {
    if (idx >= filtered.length) setIdx(Math.max(0, filtered.length - 1));
  }, [filtered.length, idx]);

  useInput((input, key) => {
    if (key.escape) {
      if (query) {
        setQuery("");
      } else {
        onCancel();
      }
      return;
    }
    if (key.upArrow) {
      setIdx((i) => Math.max(0, i - 1));
      return;
    }
    if (key.downArrow) {
      setIdx((i) => Math.min(filtered.length - 1, i + 1));
      return;
    }
    if (key.return) {
      const r = filtered[idx];
      if (r) onSelect(r.id);
      return;
    }
    if (key.ctrl && input === "r") {
      refresh();
      return;
    }
    if (key.backspace || key.delete) {
      setQuery((q) => q.slice(0, -1));
      return;
    }
    if (input && !key.ctrl && !key.meta && input.length > 0) {
      const ch = input;
      if (ch.charCodeAt(0) >= 0x20) {
        setQuery((q) => q + ch);
      }
    }
  });

  const sliceStart = Math.max(
    0,
    Math.min(filtered.length - visibleRows, idx - Math.floor(visibleRows / 2)),
  );
  const sliceEnd = Math.min(filtered.length, sliceStart + visibleRows);
  const visible = filtered.slice(sliceStart, sliceEnd);
  const overflowAbove = sliceStart > 0 ? sliceStart : 0;
  const overflowBelow = filtered.length - sliceEnd;

  return (
    <Box flexDirection="column">
      <Text bold>Resume — 최근 Claude 세션</Text>
      {loading && <Text dimColor>로딩 중...</Text>}
      {error && <Text color="red">{error}</Text>}
      {query && (
        <Box>
          <Text color="yellow">검색: </Text>
          <Text>{query}</Text>
          <Text dimColor>  ({filtered.length}/{items.length})</Text>
        </Box>
      )}
      <Box flexDirection="column" marginTop={1}>
        {!loading && filtered.length === 0 && !query && (
          <Text dimColor>(없음 — Claude 가 한 번도 실행되지 않았거나 history 부재)</Text>
        )}
        {!loading && filtered.length === 0 && query && <Text dimColor>(매치 없음)</Text>}
        {overflowAbove > 0 && <Text dimColor>  … ({overflowAbove} 더)</Text>}
        {visible.map((r, i) => {
          const realI = sliceStart + i;
          const cursor = realI === idx ? "▶" : " ";
          const proj = r.project.padEnd(20).slice(0, 20);
          // title 안의 줄바꿈 / 연속 공백을 단일 space 로 평탄화. 한 줄짜리 요약.
          const titleOneLine = r.title.replace(/\s+/g, " ").trim();
          const colorProps = realI === idx ? { color: "cyan" as const } : {};
          // Box width="100%" + Text wrap="truncate-end" 로 ink 가 cell 폭 (wide char,
          // 한글 등) 을 고려해 한 줄에 맞게 자른다.
          return (
            <Box key={r.id} width="100%">
              <Text wrap="truncate-end" {...colorProps}>
                {cursor} {proj} {r.mtimeText.padEnd(12)} {titleOneLine}
              </Text>
            </Box>
          );
        })}
        {overflowBelow > 0 && <Text dimColor>  … ({overflowBelow} 더)</Text>}
      </Box>
      <Box marginTop={1}>
        <Text dimColor>
          (입력→검색) · ↑↓ 이동 · Enter resume · Ctrl-R 새로고침 · Esc 취소
        </Text>
      </Box>
    </Box>
  );
}
