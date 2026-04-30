/**
 * 디렉토리 browser — new session 의 cwd 선택용.
 *
 * Type-ahead UX: 글자 입력하면 그대로 query 에 누적 → entries 즉시 필터링.
 * 별도 검색 모드 진입 키 없음 (Claude 의 @ 멘션 UI 패턴).
 *
 * 절대경로 자동 분해: query 가 `/` 또는 `~` 로 시작하면 마지막 `/` 까지를 cwd,
 * 그 이후를 filter 로 분리해서 cwd 를 즉시 점프시킨다 (Finder / ranger 스타일).
 *  ·  `/Volumes/Mac` paste → cwd=`/Volumes`, filter=`Mac`
 *  ·  `~/code/cl`         → cwd=`$HOME/code`, filter=`cl`
 *
 * 키:
 *   (글자 입력)        query 에 append → 필터 (또는 절대경로 점프)
 *   Backspace          query 한 글자 삭제
 *   ↑ ↓                항목 선택 (filtered 기준)
 *   Enter              폴더 진입 (진입 시 query 자동 clear)
 *   ←                  부모 디렉토리로
 *   Space              현재 cwd 로 확정 → onConfirm(cwd)
 *   Tab                hidden 디렉토리 표시 토글
 *   Esc                query 가 있으면 query clear, 없으면 cancel
 */

import { Box, Text, useInput, useStdout } from "ink";
import { useState, useMemo, useEffect } from "react";
import { readdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { resolve } from "node:path";

type Props = {
  initialCwd?: string;
  onConfirm: (cwd: string) => void;
  onCancel: () => void;
};

type Entry = { name: string; path: string };

// 헤더 / cwd 표시 / 검색 / overflow / 안내 등 고정 영역 = 약 10 줄.
const RESERVED_ROWS = 10;
const MIN_VISIBLE = 5;

function listDirs(dir: string, showHidden: boolean): Entry[] {
  let items: string[];
  try {
    items = readdirSync(dir);
  } catch {
    return [];
  }
  const out: Entry[] = [];
  for (const name of items) {
    if (!showHidden && name.startsWith(".")) continue;
    const path = resolve(dir, name);
    try {
      const st = statSync(path);
      if (st.isDirectory()) out.push({ name, path });
    } catch {
      // permission denied / broken symlink — 무시
    }
  }
  out.sort((a, b) => a.name.localeCompare(b.name));
  return out;
}

export function DirBrowser({ initialCwd, onConfirm, onCancel }: Props) {
  const [cwd, setCwd] = useState(initialCwd ?? homedir());
  const [showHidden, setShowHidden] = useState(false);
  const [query, setQuery] = useState("");
  const { stdout } = useStdout();
  const termRows = stdout?.rows ?? 24;
  const visibleRows = Math.max(MIN_VISIBLE, termRows - RESERVED_ROWS);
  const entries = useMemo(() => listDirs(cwd, showHidden), [cwd, showHidden]);
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return entries;
    return entries.filter((e) => e.name.toLowerCase().includes(q));
  }, [entries, query]);
  const [idx, setIdx] = useState(0);

  // cwd 또는 query 바뀌면 커서 reset.
  useEffect(() => {
    setIdx(0);
  }, [cwd, query]);

  // filtered 가 줄어들면 idx clamp.
  useEffect(() => {
    if (idx >= filtered.length) setIdx(Math.max(0, filtered.length - 1));
  }, [filtered.length, idx]);

  // query 누적 시 절대경로 / HOME 자동 분해.
  // next 가 `/foo/bar` 또는 `~/foo/bar` 형태면 마지막 `/` 이전을 cwd, 그 이후를
  // filter 로 분리. dir 부분이 실제 디렉토리일 때만 적용. 부재하면 next 그대로 둠.
  function appendInput(rawNext: string): void {
    let target: string | undefined;
    if (rawNext.startsWith("~/")) target = homedir() + rawNext.slice(1);
    else if (rawNext === "~") target = homedir();
    else if (rawNext.startsWith("/")) target = rawNext;

    if (target) {
      const lastSlash = target.lastIndexOf("/");
      if (lastSlash >= 0) {
        const dirPart = lastSlash === 0 ? "/" : target.slice(0, lastSlash);
        const filter = target.slice(lastSlash + 1);
        try {
          const st = statSync(dirPart);
          if (st.isDirectory()) {
            setCwd(dirPart);
            setQuery(filter);
            return;
          }
        } catch {
          // 부재 — fallthrough → query 그대로
        }
      }
    }
    setQuery(rawNext);
  }

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
      const e = filtered[idx];
      if (e) {
        setCwd(e.path);
        setQuery("");
      }
      return;
    }
    if (key.leftArrow) {
      const parent = resolve(cwd, "..");
      if (parent !== cwd) {
        setCwd(parent);
        setQuery("");
      }
      return;
    }
    if (input === " ") {
      onConfirm(cwd);
      return;
    }
    if (key.tab) {
      setShowHidden((v) => !v);
      return;
    }
    if (key.backspace || key.delete) {
      setQuery((q) => q.slice(0, -1));
      return;
    }
    // 일반 char (printable ASCII / 한글) → query 누적 (절대경로면 즉시 분해).
    if (input && !key.ctrl && !key.meta && input.length > 0) {
      if (input.charCodeAt(0) >= 0x20) {
        appendInput(query + input);
      }
    }
  });

  // 화면에 보일 슬라이스. idx 를 항상 가운데에 두려고 함.
  const sliceStart = Math.max(0, Math.min(filtered.length - visibleRows, idx - Math.floor(visibleRows / 2)));
  const sliceEnd = Math.min(filtered.length, sliceStart + visibleRows);
  const visible = filtered.slice(sliceStart, sliceEnd);
  const overflowAbove = sliceStart > 0 ? sliceStart : 0;
  const overflowBelow = filtered.length - sliceEnd;

  return (
    <Box flexDirection="column">
      <Text bold>New session — cwd 선택</Text>
      <Box marginTop={1}>
        <Text color="green">▸ </Text>
        <Text>{cwd}</Text>
      </Box>
      {query && (
        <Box>
          <Text color="yellow">검색: </Text>
          <Text>{query}</Text>
          <Text dimColor>  ({filtered.length}/{entries.length})</Text>
        </Box>
      )}
      <Box flexDirection="column" marginTop={1}>
        {filtered.length === 0 && !query && <Text dimColor>(빈 디렉토리)</Text>}
        {filtered.length === 0 && query && <Text dimColor>(매치 없음)</Text>}
        {overflowAbove > 0 && <Text dimColor>  … ({overflowAbove} 더)</Text>}
        {visible.map((e, i) => {
          const realI = sliceStart + i;
          const cursor = realI === idx ? "▶" : " ";
          const colorProps = realI === idx ? { color: "cyan" as const } : {};
          return (
            <Text key={e.path} {...colorProps}>
              {cursor} {e.name}/
            </Text>
          );
        })}
        {overflowBelow > 0 && <Text dimColor>  … ({overflowBelow} 더)</Text>}
      </Box>
      <Box marginTop={1}>
        <Text dimColor>
          (입력→검색) · ↑↓ 이동 · Enter 진입 · ← 부모 · Space 확정 · Tab hidden · Esc 취소
        </Text>
      </Box>
    </Box>
  );
}
