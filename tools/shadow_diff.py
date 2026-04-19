"""Shadow-diff 판정기 — step-3-design §13.11.a 집합 비교 기반.

기존 "±5% block_commit 개수" 비교는 약한 신호 (parser 가 통째로 놓친 case-04
같은 버그에서는 둘이 일치한다 = 둘 다 틀림 이 되기 때문). 대신 세 집합을
분리 보고한다:

    parser ∩ analyzer  → 공통 (정상, sanity log)
    parser - analyzer  → regression 후보. **반드시 0건.** 1건이라도 있으면
                         analyzer 측 gap 이므로 조사 필수.
    analyzer - parser  → 신규 탐지. case-04 유형. 0 건 이상 허용 (목표).

판정 단일 불변식: `parser - analyzer = ∅` 이면 PASS.

Fingerprint:
    block_commit / flush_block 텍스트의 첫 `⏺ ` prefix 제거 후 공백 정규화 → 60자.

사용:
    python -m tools.shadow_diff \\
        --parser-events <events.jsonl> \\
        --analyzer-events <analyzer-events.jsonl>
    # 또는 raw 에서 analyzer 를 on-the-fly 로 돌림:
    python -m tools.shadow_diff \\
        --parser-events <events.jsonl> --raw <raw.log>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


_WS_RE = re.compile(r"\s+")


def fingerprint(text: str, *, width: int = 60) -> str:
    """비교 가능한 정규화 키.

    `⏺` 블록의 **첫 줄만** 사용한다. 본문 continuation 은 parser / analyzer 의
    커밋 경계 해석 차이 (tool result 병합 vs Claude 활동 라인 병합) 로 달라지는데,
    block 식별의 충분조건은 header 한 줄이다. 이후 `⏺ ` prefix 제거 + 공백 축약
    + width 자르기.
    """
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if first_line.startswith("⏺"):
        first_line = first_line[1:].lstrip()
    return _WS_RE.sub(" ", first_line.strip())[:width]


# ---- parser side ----------------------------------------------------------

def iter_parser_blocks(events_path: Path) -> Iterable[tuple[str, str]]:
    """parser 의 dispatched `⏺` block 들을 (fingerprint, preview) 로 yield.

    `flush_block` 이벤트를 블록의 권위 있는 발화 시점으로 본다. `send_output`
    은 재전송 / include_last 재렌더 / 단일 send 를 모두 포함해 noise 가 많다.
    비교 fingerprint 는 `⏺` prefix 가 붙은 preview 만 (analyzer 와 대조 가능).
    """
    with events_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("kind") != "flush_block":
                continue
            preview = (e.get("preview") or "").strip()
            if not preview.startswith("⏺"):
                # `⏺` 가 없는 block preview 는 analyzer 범위 밖 (busy label 등)
                continue
            yield fingerprint(preview), preview


# ---- analyzer side --------------------------------------------------------

def iter_analyzer_blocks_from_file(
    analyzer_events_path: Path,
) -> Iterable[tuple[str, str]]:
    with analyzer_events_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("t") != "block_commit":
                continue
            text = (e.get("text") or "").strip()
            if not text.startswith("⏺"):
                continue
            yield fingerprint(text), text


def iter_analyzer_blocks_from_raw(
    raw_path: Path, rotate_before: list[Path] | None = None
) -> Iterable[tuple[str, str]]:
    # 지연 import: 모듈 load 시 tokenizer/classifier pipeline 을 끌고 오지
    # 않도록 필요할 때만 import 한다.
    from tools.pipe_pane_analyzer import analyze_stream

    for ev in analyze_stream(raw_path, rotate_before):
        if ev.t != "block_commit":
            continue
        text = (ev.payload.get("text") or "").strip()
        if not text.startswith("⏺"):
            continue
        yield fingerprint(text), text


# ---- diff core ------------------------------------------------------------

@dataclass
class ShadowDiffReport:
    parser_fps: set[str] = field(default_factory=set)
    analyzer_fps: set[str] = field(default_factory=set)
    # fp → representative preview (첫 등장)
    parser_samples: dict[str, str] = field(default_factory=dict)
    analyzer_samples: dict[str, str] = field(default_factory=dict)

    @property
    def common(self) -> set[str]:
        return self.parser_fps & self.analyzer_fps

    @property
    def parser_only(self) -> set[str]:
        return self.parser_fps - self.analyzer_fps

    @property
    def analyzer_only(self) -> set[str]:
        return self.analyzer_fps - self.parser_fps

    @property
    def passed(self) -> bool:
        """§13.11.a 단일 불변식: parser - analyzer = ∅."""
        return not self.parser_only

    def to_dict(self) -> dict:
        def _render(fps: set[str], samples: dict[str, str]) -> list[dict]:
            return [{"fp": fp, "sample": samples.get(fp, "")} for fp in sorted(fps)]

        return {
            "verdict": "PASS" if self.passed else "FAIL",
            "counts": {
                "parser_total": len(self.parser_fps),
                "analyzer_total": len(self.analyzer_fps),
                "common": len(self.common),
                "parser_only": len(self.parser_only),
                "analyzer_only": len(self.analyzer_only),
            },
            "parser_only": _render(self.parser_only, self.parser_samples),
            "analyzer_only": _render(self.analyzer_only, self.analyzer_samples),
            "common": sorted(self.common),
        }


def build_report(
    parser_blocks: Iterable[tuple[str, str]],
    analyzer_blocks: Iterable[tuple[str, str]],
) -> ShadowDiffReport:
    r = ShadowDiffReport()
    for fp, preview in parser_blocks:
        if fp in r.parser_fps:
            continue
        r.parser_fps.add(fp)
        r.parser_samples[fp] = preview
    for fp, text in analyzer_blocks:
        if fp in r.analyzer_fps:
            continue
        r.analyzer_fps.add(fp)
        r.analyzer_samples[fp] = text
    return r


# ---- CLI ------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    parser_events: Path = args.parser_events
    if not parser_events.is_file():
        print(f"error: --parser-events not found: {parser_events}", file=sys.stderr)
        return 2

    if args.analyzer_events is None and args.raw is None:
        print(
            "error: one of --analyzer-events or --raw required",
            file=sys.stderr,
        )
        return 2
    if args.analyzer_events is not None and args.raw is not None:
        print(
            "error: --analyzer-events and --raw are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    p_blocks = iter_parser_blocks(parser_events)
    if args.analyzer_events is not None:
        if not args.analyzer_events.is_file():
            print(
                f"error: --analyzer-events not found: {args.analyzer_events}",
                file=sys.stderr,
            )
            return 2
        a_blocks = iter_analyzer_blocks_from_file(args.analyzer_events)
    else:
        if not args.raw.is_file():
            print(f"error: --raw not found: {args.raw}", file=sys.stderr)
            return 2
        a_blocks = iter_analyzer_blocks_from_raw(
            args.raw, args.rotate_before or None
        )

    report = build_report(p_blocks, a_blocks)

    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))

    # §13.11.a: 기존 "±5% 개수" 는 sanity log 로만 (FAIL 트리거 아님).
    pt, at = len(report.parser_fps), len(report.analyzer_fps)
    if pt:
        ratio = at / pt
        print(
            f"[sanity] analyzer/parser block ratio = {ratio:.2f} "
            f"(parser={pt} analyzer={at})",
            file=sys.stderr,
        )

    return 0 if report.passed else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shadow_diff",
        description=(
            "Set-based shadow diff between bridge parser (events.jsonl) "
            "and analyzer (block_commit events). Verdict invariant: "
            "parser - analyzer = empty."
        ),
    )
    p.add_argument(
        "--parser-events", type=Path, required=True,
        help="bridge events.jsonl (dump.event output)",
    )
    p.add_argument(
        "--analyzer-events", type=Path, default=None,
        help="pre-computed analyzer events JSONL",
    )
    p.add_argument(
        "--raw", type=Path, default=None,
        help="raw pipe-pane log; analyzer runs on-the-fly",
    )
    p.add_argument(
        "--rotate-before", type=Path, nargs="*", default=None,
        help="previous rotated raw logs (only with --raw)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
