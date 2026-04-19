"""shadow_diff self-test — step-3-design §13.11.a.

단일 불변식: parser - analyzer = ∅.

case-04 는 parser 가 Edit 승인 다이얼로그를 **놓친** fixture 이지만, 놓친 쪽은
analyzer(+) / parser(-) 축이다. parser 가 실제로 dispatch 한 `⏺` 블록들은 analyzer
가 모두 잡아야 한다 → 불변식 PASS 여야 한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import shadow_diff


FIXTURE = Path("tools/fixtures/baseline/case-04-edit-approval-wording")


def _skip_if_missing() -> tuple[Path, Path]:
    events = FIXTURE / "events.jsonl"
    raw = FIXTURE / "pipe-pane-raw.log"
    if not events.is_file() or not raw.is_file():
        pytest.skip("case-04 fixture not present")
    return events, raw


def test_fingerprint_uses_only_first_line():
    """block continuation 은 parser / analyzer 커밋 경계 차이로 달라지므로
    fingerprint 는 header 한 줄만 쓴다."""
    a = shadow_diff.fingerprint("⏺ Reading 1 file…\n  ⎿ parser tool result")
    b = shadow_diff.fingerprint("⏺ Reading 1 file…\n  analyzer continuation")
    assert a == b == "Reading 1 file…"


def test_fingerprint_strips_pulse_prefix():
    a = shadow_diff.fingerprint("⏺   Hello world")
    b = shadow_diff.fingerprint("Hello world")
    assert a == b == "Hello world"


def test_fingerprint_collapses_internal_whitespace():
    fp = shadow_diff.fingerprint("⏺ foo   bar\tbaz")
    assert fp == "foo bar baz"


def test_fingerprint_width_truncation():
    long = "⏺ " + "a" * 200
    fp = shadow_diff.fingerprint(long, width=60)
    assert len(fp) == 60


def test_iter_parser_blocks_filters_non_pulse(tmp_path: Path):
    p = tmp_path / "events.jsonl"
    lines = [
        {"kind": "flush_block", "preview": "⏺ 응, 커밋 완료"},
        {"kind": "flush_block", "preview": "no pulse prefix — busy label"},
        {"kind": "send_output", "preview": "⏺ 다른 텍스트"},  # flush_block 만 본다
        {"kind": "on_message", "caption": "user text"},
    ]
    p.write_text(
        "\n".join(json.dumps(l, ensure_ascii=False) for l in lines), encoding="utf-8"
    )
    out = list(shadow_diff.iter_parser_blocks(p))
    assert len(out) == 1
    assert out[0][1].startswith("⏺ 응, 커밋")


def test_build_report_basic():
    parser = [("fp-a", "⏺ A"), ("fp-b", "⏺ B"), ("fp-a", "⏺ A dup")]
    analyzer = [("fp-a", "⏺ A"), ("fp-c", "⏺ C")]
    r = shadow_diff.build_report(parser, analyzer)
    assert r.parser_fps == {"fp-a", "fp-b"}
    assert r.analyzer_fps == {"fp-a", "fp-c"}
    assert r.common == {"fp-a"}
    assert r.parser_only == {"fp-b"}
    assert r.analyzer_only == {"fp-c"}
    assert r.passed is False  # parser_only 가 있으면 FAIL


def test_build_report_passes_when_parser_subset_of_analyzer():
    parser = [("a", "⏺ A"), ("b", "⏺ B")]
    analyzer = [("a", "⏺ A"), ("b", "⏺ B"), ("c", "⏺ extra")]
    r = shadow_diff.build_report(parser, analyzer)
    assert r.passed is True
    assert r.analyzer_only == {"c"}
    assert r.parser_only == set()


def test_case_04_invariant_holds():
    """case-04 에서 parser - analyzer = ∅ 이다 (단일 불변식)."""
    events, raw = _skip_if_missing()

    p_blocks = list(shadow_diff.iter_parser_blocks(events))
    a_blocks = list(shadow_diff.iter_analyzer_blocks_from_raw(raw))

    report = shadow_diff.build_report(p_blocks, a_blocks)

    # 메시지 풍부화: 실패 시 어느 preview 가 analyzer 에서 누락됐는지 출력.
    if report.parser_only:
        missing = [report.parser_samples.get(fp, fp) for fp in report.parser_only]
        pytest.fail(
            "parser - analyzer must be empty (§13.11.a). "
            f"missing from analyzer: {missing!r}"
        )
    assert report.passed is True


def test_case_04_analyzer_detects_more_than_parser():
    """case-04 의 가치: analyzer - parser ≠ ∅ (신규 탐지).

    parser 는 이 fixture 에서 2~3 개 `⏺` block 만 dispatch 했으나 analyzer 는
    Edit 승인 다이얼로그 앞뒤로 Claude 가 찍은 모든 `⏺` 블록을 본다.
    """
    events, raw = _skip_if_missing()

    p_blocks = list(shadow_diff.iter_parser_blocks(events))
    a_blocks = list(shadow_diff.iter_analyzer_blocks_from_raw(raw))

    report = shadow_diff.build_report(p_blocks, a_blocks)
    assert len(report.analyzer_only) > 0, (
        "analyzer 가 parser 보다 더 많은 block 을 탐지해야 case-04 의 가치가 성립한다. "
        f"parser={len(report.parser_fps)} analyzer={len(report.analyzer_fps)}"
    )


def test_cli_returns_nonzero_on_failure(tmp_path: Path):
    """CLI 는 parser_only > 0 이면 exit code 1."""
    parser_ev = tmp_path / "events.jsonl"
    analyzer_ev = tmp_path / "analyzer.jsonl"
    # parser 만 가진 blocks
    parser_ev.write_text(
        json.dumps({"kind": "flush_block", "preview": "⏺ unique to parser"}) + "\n",
        encoding="utf-8",
    )
    analyzer_ev.write_text(
        json.dumps({"t": "block_commit", "text": "⏺ different block"}) + "\n",
        encoding="utf-8",
    )
    rc = shadow_diff.main([
        "--parser-events", str(parser_ev),
        "--analyzer-events", str(analyzer_ev),
    ])
    assert rc == 1


def test_cli_returns_zero_when_analyzer_is_superset(tmp_path: Path, capsys):
    parser_ev = tmp_path / "events.jsonl"
    analyzer_ev = tmp_path / "analyzer.jsonl"
    parser_ev.write_text(
        json.dumps({"kind": "flush_block", "preview": "⏺ shared"}) + "\n",
        encoding="utf-8",
    )
    analyzer_ev.write_text(
        "\n".join([
            json.dumps({"t": "block_commit", "text": "⏺ shared"}),
            json.dumps({"t": "block_commit", "text": "⏺ extra from analyzer"}),
        ]) + "\n",
        encoding="utf-8",
    )
    rc = shadow_diff.main([
        "--parser-events", str(parser_ev),
        "--analyzer-events", str(analyzer_ev),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["verdict"] == "PASS"
    assert data["counts"]["analyzer_only"] == 1
