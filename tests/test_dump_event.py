"""
20260417_163701 회귀 방지 — dump.event() positional+kwarg `kind` 충돌.

bridge/core.py 의 queue_slip 경로가 `dump.event("core", "queue_slip", kind=slip, ...)`
로 positional `kind` 와 keyword `kind` 를 동시에 넘겨 TypeError 를 유발했다.
이 에러가 monitor 루프에서 반복 발생해 MONITOR-ERROR 10회 누적 후 종료되던
회귀를 방지하고, `dump.event()` 의 호출 계약을 고정한다.

수정안: slip 종류는 `slip_kind` 필드로 기록. positional `kind` 는 "queue_slip".
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import dump  # noqa: E402


def test_event_accepts_slip_kind_kwarg():
    """정상 호출 — slip_kind 는 kwarg 로 자유롭게 전달 가능."""
    dump.event(
        "core", "queue_slip",
        tag="test", slip_kind="idx_past_cc",
        idx=5, cc=0, last_fp="⏺ prev",
    )


def test_event_rejects_duplicate_kind():
    """계약: positional `kind` 와 keyword `kind` 동시 전달 시 TypeError.

    이 에러가 잡혀야 core.py 에서 `kind=slip` 이 아니라 `slip_kind=slip` 을 쓰도록
    호출 측이 강제된다.
    """
    with pytest.raises(TypeError):
        dump.event("core", "queue_slip", kind="dup")
