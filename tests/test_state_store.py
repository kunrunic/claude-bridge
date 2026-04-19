"""§13.4 State atomic write + 2-version — tools/state_store.py.

핵심 속성:
  - save → load round-trip
  - .current 가 손상돼도 .prev 로 fallback
  - 이전 .current 가 save 후 .prev 로 rotate
  - 중도 실패(write 중 예외)가 기존 파일을 오염시키지 않음
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from tools.state_store import StateStore


def test_save_and_load_roundtrip(tmp_path: Path):
    s = StateStore(tmp_path / "analyzer_state")
    s.save({"offset": 42, "inode": 1234})
    assert s.load() == {"offset": 42, "inode": 1234}


def test_load_returns_none_when_no_file(tmp_path: Path):
    s = StateStore(tmp_path / "analyzer_state")
    assert s.load() is None


def test_save_rotates_current_into_prev(tmp_path: Path):
    s = StateStore(tmp_path / "analyzer_state")
    s.save({"v": 1})
    s.save({"v": 2})
    assert s.current_path.exists()
    assert s.prev_path.exists()
    # .current 는 최신, .prev 는 직전
    import json
    assert json.loads(s.current_path.read_text()) == {"v": 2}
    assert json.loads(s.prev_path.read_text()) == {"v": 1}


def test_load_falls_back_to_prev_when_current_corrupt(tmp_path: Path):
    s = StateStore(tmp_path / "analyzer_state")
    s.save({"v": 1})
    s.save({"v": 2})
    # .current 를 손상시킨다
    s.current_path.write_text("{ invalid json ", encoding="utf-8")
    # .prev 로 fallback
    assert s.load() == {"v": 1}


def test_load_falls_back_to_prev_when_current_missing(tmp_path: Path):
    s = StateStore(tmp_path / "analyzer_state")
    s.save({"v": 1})
    s.save({"v": 2})
    s.current_path.unlink()
    assert s.load() == {"v": 1}


def test_save_creates_parent_dir(tmp_path: Path):
    base = tmp_path / "sub" / "dir" / "analyzer_state"
    s = StateStore(base)
    s.save({"x": 1})
    assert s.current_path.exists()


def test_save_failure_does_not_corrupt_current(tmp_path: Path):
    """write 중 예외가 발생해도 기존 .current 는 유효해야 한다."""
    s = StateStore(tmp_path / "analyzer_state")
    s.save({"v": "original"})

    # json.dump 을 예외로 패치
    with patch("tools.state_store.json.dump", side_effect=IOError("disk full")):
        with pytest.raises(IOError):
            s.save({"v": "bad"})

    # 원래 state 유지
    assert s.load() == {"v": "original"}
    # tmp 파일 잔해 없음
    leftover = list(tmp_path.glob("analyzer_state.*.tmp"))
    assert leftover == [], f"leftover tmp files: {leftover}"


def test_clear_removes_both_versions(tmp_path: Path):
    s = StateStore(tmp_path / "analyzer_state")
    s.save({"v": 1})
    s.save({"v": 2})
    s.clear()
    assert not s.current_path.exists()
    assert not s.prev_path.exists()
    assert s.load() is None


def test_save_uses_atomic_rename(tmp_path: Path):
    """사용 중인 reader 가 절반-쓰인 파일을 보지 않도록 tmp→rename 경로여야.

    os.replace 호출을 세어 .current 를 한 번에 교체하는지 간접 검증.
    """
    s = StateStore(tmp_path / "state")
    s.save({"v": 1})

    calls: list[tuple] = []
    import os
    real_replace = os.replace

    def spy(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    with patch("tools.state_store.os.replace", side_effect=spy):
        s.save({"v": 2})

    # 두 번의 replace: current→prev, tmp→current
    assert len(calls) == 2
    assert calls[0][1].endswith(".prev")
    assert calls[1][1].endswith(".current")


def test_non_ascii_roundtrip(tmp_path: Path):
    s = StateStore(tmp_path / "state")
    s.save({"msg": "한글 메시지", "emoji": "⏺ 승인"})
    assert s.load() == {"msg": "한글 메시지", "emoji": "⏺ 승인"}
