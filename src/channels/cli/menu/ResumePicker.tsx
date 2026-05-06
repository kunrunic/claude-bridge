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

import { Box, Text, useInput, measureElement, type DOMElement } from "ink";
import { useState, useEffect, useMemo, useRef } from "react";
import type { CliRecentInfo } from "../../../core/ipc.ts";
import { rpc } from "./rpc.ts";

type Props = {
  onSelect: (id: string) => void;
  onCancel: () => void;
};

// list 영역 가용 줄 수는 ink flex layout 이 분배한 값을 measureElement 로 직접 측정 →
// chrome 줄 수를 일일이 계산해서 빼는 하드코딩이 사라진다. 첫 frame 측정 전 fallback.
const MIN_VISIBLE = 5;

export function ResumePicker({ onSelect, onCancel }: Props) {
  const [items, setItems] = useState<CliRecentInfo[]>([]);
  const [idx, setIdx] = useState(0);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const listRef = useRef<DOMElement>(null);
  const [listRows, setListRows] = useState(MIN_VISIBLE + 2);

  // ink flex layout 이 list 박스에 실제 할당한 높이를 매 render 측정. App chrome
  // 변화(error/info, padding, header)나 터미널 resize 에 자동 적응.
  useEffect(() => {
    if (!listRef.current) return;
    const { height } = measureElement(listRef.current);
    if (height > 0 && height !== listRows) setListRows(height);
  });

  // overflowAbove / overflowBelow 슬롯 2 줄을 항목 영역에서 뺀다.
  const visibleRows = Math.max(MIN_VISIBLE, listRows - 2);

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
    <Box flexDirection="column" flexGrow={1}>
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
      <Box flexDirection="column" marginTop={1} flexGrow={1} ref={listRef}>
        {/* flex 가 분배한 가용 높이만큼 빈 줄로 패딩해 출력 줄 수 고정. 모드 전환·필터
            변동 어느 경우에도 박스 높이가 안 변하므로 잔여물 없음. */}
        <Text dimColor>{overflowAbove > 0 ? `  … (${overflowAbove} 더)` : " "}</Text>
        {Array.from({ length: visibleRows }, (_, i) => {
          const r = visible[i];
          if (!r) {
            if (i === 0 && !loading && filtered.length === 0) {
              return (
                <Text key="empty" dimColor>
                  {query
                    ? "  (매치 없음)"
                    : "  (없음 — Claude 가 한 번도 실행되지 않았거나 history 부재)"}
                </Text>
              );
            }
            return <Text key={`pad-${i}`}> </Text>;
          }
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
        <Text dimColor>{overflowBelow > 0 ? `  … (${overflowBelow} 더)` : " "}</Text>
      </Box>
      <Box marginTop={1}>
        <Text dimColor>
          (입력→검색) · ↑↓ 이동 · Enter resume · Ctrl-R 새로고침 · Esc 취소
        </Text>
      </Box>
    </Box>
  );
}
