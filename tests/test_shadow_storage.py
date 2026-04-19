"""§13.5 C5 — Shadow run storage 분리.

분석기 이벤트는 bridge 의 events.jsonl 과 **별도 파일** 에 기록한다
(frozen parser 보호). 같은 세션 디렉토리에 analyzer-events.jsonl +
shadow-diff.jsonl 이 위치한다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tools import shadow_storage as ss


# -- 경로 규칙 ------------------------------------------------------------

def test_paths_follow_panes_convention(tmp_path: Path):
    d = ss.session_dir("claude_bridge", base=tmp_path)
    assert d == tmp_path / "claude_bridge"
    assert ss.analyzer_events_path("claude_bridge", base=tmp_path) == (
        tmp_path / "claude_bridge" / "analyzer-events.jsonl"
    )
    assert ss.shadow_diff_path("claude_bridge", base=tmp_path) == (
        tmp_path / "claude_bridge" / "shadow-diff.jsonl"
    )


def test_session_dir_is_per_session(tmp_path: Path):
    a = ss.session_dir("sess-a", base=tmp_path)
    b = ss.session_dir("sess-b", base=tmp_path)
    assert a != b


# -- JSONLAppender --------------------------------------------------------

def test_appender_creates_parent_dir(tmp_path: Path):
    with ss.open_analyzer_events("sess-x", base=tmp_path) as w:
        w.write({"t": "block_commit", "offset": 1})
    assert ss.analyzer_events_path("sess-x", base=tmp_path).exists()


def test_appender_round_trip(tmp_path: Path):
    records = [
        {"t": "session_boot", "offset": 0},
        {"t": "block_commit", "offset": 120, "text": "⏺ 한글 OK"},
    ]
    with ss.open_analyzer_events("sess", base=tmp_path) as w:
        w.write_many(records)

    path = ss.analyzer_events_path("sess", base=tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == records[0]
    assert json.loads(lines[1]) == records[1]


def test_appender_is_append_only(tmp_path: Path):
    """두 번 연 session 이 데이터를 덮어쓰지 않고 뒤에 append."""
    with ss.open_shadow_diff("sess", base=tmp_path) as w:
        w.write({"verdict": "PASS", "run": 1})
    with ss.open_shadow_diff("sess", base=tmp_path) as w:
        w.write({"verdict": "PASS", "run": 2})

    path = ss.shadow_diff_path("sess", base=tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["run"] == 1
    assert json.loads(lines[1])["run"] == 2


def test_appender_write_outside_context_raises(tmp_path: Path):
    w = ss.JSONLAppender(tmp_path / "x.jsonl")
    with pytest.raises(RuntimeError):
        w.write({"x": 1})


def test_appender_serializes_dataclass_with_to_dict(tmp_path: Path):
    @dataclass
    class _Ev:
        offset: int
        t: str
        payload: dict = field(default_factory=dict)

        def to_dict(self) -> dict:
            return {"offset": self.offset, "t": self.t, **self.payload}

    with ss.open_analyzer_events("sess", base=tmp_path) as w:
        w.write(_Ev(42, "block_commit", {"kind": "response"}))

    line = ss.analyzer_events_path("sess", base=tmp_path).read_text().strip()
    assert json.loads(line) == {"offset": 42, "t": "block_commit", "kind": "response"}


def test_appender_serializes_plain_dataclass_via_asdict(tmp_path: Path):
    @dataclass
    class _Ev:
        offset: int
        t: str

    with ss.open_analyzer_events("sess", base=tmp_path) as w:
        w.write(_Ev(7, "busy_enter"))

    line = ss.analyzer_events_path("sess", base=tmp_path).read_text().strip()
    assert json.loads(line) == {"offset": 7, "t": "busy_enter"}


# -- 분리 보장 -----------------------------------------------------------

def test_analyzer_and_parser_events_are_different_paths(tmp_path: Path):
    """analyzer-events.jsonl 이 bridge 의 events.jsonl 이름과 달라야 한다
    (frozen parser 보호의 핵심 전제)."""
    p = ss.analyzer_events_path("sess", base=tmp_path).name
    assert p == "analyzer-events.jsonl"
    assert p != "events.jsonl"


# -- cutover rename ------------------------------------------------------

def test_cutover_archives_shadow_diff_only(tmp_path: Path):
    with ss.open_shadow_diff("sess", base=tmp_path) as w:
        w.write({"v": 1})
    with ss.open_analyzer_events("sess", base=tmp_path) as w:
        w.write({"v": 2})

    result = ss.cutover_rename("sess", base=tmp_path)

    # shadow-diff 는 archived
    assert len(result["archived"]) == 1
    assert "shadow-diff.archived-0.jsonl" in result["archived"][0]
    assert not ss.shadow_diff_path("sess", base=tmp_path).exists()

    # analyzer-events 는 보존 (§13.5)
    assert ss.analyzer_events_path("sess", base=tmp_path).exists()


def test_cutover_numbers_archive_suffix(tmp_path: Path):
    """여러 번 cutover / rollback 순환 시 .archived-0, -1, ... 증가."""
    for _ in range(3):
        with ss.open_shadow_diff("sess", base=tmp_path) as w:
            w.write({"x": 1})
        ss.cutover_rename("sess", base=tmp_path)

    d = ss.session_dir("sess", base=tmp_path)
    archived = sorted(p.name for p in d.glob("shadow-diff.archived-*.jsonl"))
    assert archived == [
        "shadow-diff.archived-0.jsonl",
        "shadow-diff.archived-1.jsonl",
        "shadow-diff.archived-2.jsonl",
    ]


def test_cutover_noop_when_no_shadow_diff(tmp_path: Path):
    result = ss.cutover_rename("sess-empty", base=tmp_path)
    assert result == {"archived": []}
