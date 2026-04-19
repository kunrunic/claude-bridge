"""End-to-end fixture tests — step-2-spec.md §Fixture 및 테스트.

baseline case-01~04 raw 로그를 pipe_pane_analyzer 로 흘려 이벤트 스트림이
기대대로 나오는지 검증.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.pipe_pane_analyzer import analyze_stream

FIXTURE_BASE = Path("tools/fixtures/baseline")


def _events(path: Path) -> list:
    return list(analyze_stream(path))


def _types(events: list) -> list[str]:
    return [e.t for e in events]


def test_case_04_edit_approval_detected():
    """case-04: Edit 승인 다이얼로그가 approval_show(tool_hint='edit') 로 감지."""
    raw = FIXTURE_BASE / "case-04-edit-approval-wording" / "pipe-pane-raw.log"
    if not raw.is_file():
        pytest.skip("case-04 fixture not present")
    events = _events(raw)
    approvals = [e for e in events if e.t == "approval_show"]
    assert len(approvals) >= 1, "expected at least one approval_show"
    # 적어도 하나는 edit tool_hint
    edit_approvals = [e for e in approvals if e.payload.get("tool_hint") == "edit"]
    assert len(edit_approvals) >= 1, (
        f"expected tool_hint='edit' but got: "
        f"{[(e.payload.get('tool_hint'), e.payload.get('summary')) for e in approvals]}"
    )


def test_case_04_block_commits_present():
    """case-04: `⏺` 블록들이 block_commit 이벤트로 emit."""
    raw = FIXTURE_BASE / "case-04-edit-approval-wording" / "pipe-pane-raw.log"
    if not raw.is_file():
        pytest.skip("case-04 fixture not present")
    events = _events(raw)
    blocks = [e for e in events if e.t == "block_commit"]
    assert len(blocks) > 10, f"expected many block_commits in case-04, got {len(blocks)}"


def test_case_04_no_queue_slip_equivalent():
    """case-04: current arch 의 queue_slip 루프에 해당하는 현상이 없어야.

    block_commit 은 snapshot 경로에서 동일 offset 에 복수(서로 다른 본문) 발화
    될 수 있으므로 (offset, t, text) 기준. 나머지 이벤트는 (offset, t) 기준."""
    raw = FIXTURE_BASE / "case-04-edit-approval-wording" / "pipe-pane-raw.log"
    if not raw.is_file():
        pytest.skip("case-04 fixture not present")
    events = _events(raw)
    keys: list[tuple] = []
    for e in events:
        if e.t == "block_commit":
            keys.append((e.offset, e.t, e.payload.get("text", "")))
        else:
            keys.append((e.offset, e.t))
    assert len(keys) == len(set(keys)), "dedup invariant violated"


def test_analyzer_meta_absent_in_stream():
    """analyzer_meta 같은 skeleton artifact 가 event stream 에 섞여있지 않아야."""
    raw = FIXTURE_BASE / "case-04-edit-approval-wording" / "pipe-pane-raw.log"
    if not raw.is_file():
        pytest.skip("case-04 fixture not present")
    events = _events(raw)
    assert not any(e.t == "analyzer_meta" for e in events)


def test_event_types_are_well_formed():
    """모든 이벤트가 spec 정의된 타입 enum 에 속해야."""
    raw = FIXTURE_BASE / "case-04-edit-approval-wording" / "pipe-pane-raw.log"
    if not raw.is_file():
        pytest.skip("case-04 fixture not present")
    allowed = {
        "block_commit",
        "approval_show",
        "approval_confirm",
        "approval_deny",
        "approval_cancel",
        "busy_enter",
        "busy_exit",
        "session_boot",
        "session_resume",
        "user_prompt",
    }
    events = _events(raw)
    actual = {e.t for e in events}
    unknown = actual - allowed
    assert not unknown, f"unknown event types: {unknown}"
