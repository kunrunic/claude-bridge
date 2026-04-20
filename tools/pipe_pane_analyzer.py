"""pipe-pane 오프라인 분석기 CLI.

docs/plans/20260419-pipe-pane-redesign-poc/step-2-spec.md 의 구현체.

파이프라인:
    raw bytes → [1] ANSI tokenizer → [2] VT screen → [3] line-commit →
    [4] region tag → [5] event classify → [6] state machine → JSON Lines

본 파일은 **CLI + orchestration**. 각 단계는 동일 패키지 하위 모듈에서
구현하고, 여기서는 조립만 한다. 프로덕션 bridge 와 독립 (import 하지 않음).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterator

from tools.ansi_tokenizer import Tokenizer
from tools.event_classifier import AnalyzerEvent, EventClassifier
from tools.line_commit import LineCommitter
from tools.region_tagger import RegionTagger


def iter_raw_bytes(
    raw_path: Path, rotate_before: list[Path] | None = None
) -> Iterator[tuple[int, bytes]]:
    """session offset 고정한 채 raw 바이트를 순서대로 yield.

    rotate_before 에 이전 rotation 파일들이 주어지면 그 파일들을 먼저 흘리고,
    세션 offset 을 이어서 raw_path 읽는다. yield 단위는 파일 read chunk 이며,
    각 tuple 은 (session_offset_at_chunk_start, chunk_bytes).
    """

    session_offset = 0
    chain: list[Path] = []
    if rotate_before:
        chain.extend(rotate_before)
    chain.append(raw_path)

    for path in chain:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    break
                yield session_offset, chunk
                session_offset += len(chunk)


def analyze_stream(
    raw_path: Path,
    rotate_before: list[Path] | None = None,
    until_offset: int | None = None,
) -> Iterator[AnalyzerEvent]:
    """파이프라인 전체를 돌려 이벤트를 yield."""

    tokenizer = Tokenizer()
    committer = LineCommitter()
    tagger = RegionTagger()
    classifier = EventClassifier()

    for session_off, chunk in iter_raw_bytes(raw_path, rotate_before):
        if until_offset is not None and session_off >= until_offset:
            break
        # chunk 를 until_offset 에 맞게 자르기
        if until_offset is not None and session_off + len(chunk) > until_offset:
            chunk = chunk[: until_offset - session_off]
        for tok in tokenizer.feed(session_off, chunk):
            for commit in committer.feed(tok):
                tagged = tagger.tag(commit)
                yield from classifier.feed(tagged)

    # flush
    for tok in tokenizer.flush():
        for commit in committer.feed(tok):
            tagged = tagger.tag(commit)
            yield from classifier.feed(tagged)
    for commit in committer.flush():
        tagged = tagger.tag(commit)
        yield from classifier.feed(tagged)
    yield from classifier.flush()


def run(args: argparse.Namespace) -> int:
    raw_path: Path = args.raw
    if not raw_path.is_file():
        print(f"error: --raw not found: {raw_path}", file=sys.stderr)
        return 2

    rotate_before: list[Path] = args.rotate_before or []
    for p in rotate_before:
        if not p.is_file():
            print(f"error: --rotate-before not found: {p}", file=sys.stderr)
            return 2

    count = 0
    for ev in analyze_stream(raw_path, rotate_before, args.until_offset):
        print(json.dumps(ev.to_dict(), ensure_ascii=False))
        count += 1

    print(f"[analyzer] emitted {count} events from {raw_path.name}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pipe_pane_analyzer",
        description="Offline analyzer for bridge pipe-pane raw logs (PoC).",
    )
    p.add_argument(
        "--raw", type=Path, required=True, help="Path to raw-*.log (current session)"
    )
    p.add_argument(
        "--rotate-before",
        type=Path,
        nargs="*",
        default=None,
        help="Previous rotated raw logs, in chronological order",
    )
    p.add_argument(
        "--events",
        type=Path,
        default=None,
        help="Companion dump/pane_tick.jsonl for cross-validation (optional)",
    )
    p.add_argument(
        "--send-input",
        type=Path,
        default=None,
        help="send_input log for user-echo correlation (optional, case-02)",
    )
    p.add_argument(
        "--until-offset",
        type=int,
        default=None,
        help="Stop analyzing when session offset >= N (debug aid)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
