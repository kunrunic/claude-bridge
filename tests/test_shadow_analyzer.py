"""ShadowAnalyzer (bridge/shadow_analyzer.py) — Step 4-α live tail 분석기.

테스트 전략:
  - 실제 pipe-pane 없이 tmp 디렉토리의 파일에 bytes 를 append 하고 poll().
  - tmux_session 은 임의 string. base 로 tmp_path 주입.
  - rotation 은 switch_file() 로 시뮬레이션.
  - resume 은 analyzer 를 stop → 새 인스턴스 start.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bridge.shadow_analyzer import ShadowAnalyzer
from tools import shadow_storage as ss


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _write_append(path: Path, data: bytes) -> None:
    with path.open("ab") as fh:
        fh.write(data)


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]


# -- 기본 ---------------------------------------------------------------------

def test_start_without_file_is_noop():
    a = ShadowAnalyzer("sess", base=Path("/tmp/cb2-test-nofile"))
    a.start(current_path=None)
    out = _run(a.poll())
    assert out == 0
    a.stop()


def test_ingests_welcome_banner_emits_session_boot(tmp_path: Path):
    raw = tmp_path / "raw-1.log"
    raw.write_bytes(b"Welcome to Claude Code\n")

    a = ShadowAnalyzer("sess", base=tmp_path)
    a.start(current_path=raw)
    read = _run(a.poll())
    assert read == len(b"Welcome to Claude Code\n")
    a.stop()

    events = _read_events(ss.analyzer_events_path("sess", base=tmp_path))
    types = [e["t"] for e in events]
    assert "session_boot" in types
    # 관측 meta 도 기록
    assert "shadow.shadow_start" in types
    assert "shadow.shadow_stop" in types


def test_poll_consumes_appended_bytes_across_calls(tmp_path: Path):
    raw = tmp_path / "raw.log"
    raw.write_bytes(b"")
    a = ShadowAnalyzer("sess", base=tmp_path)
    a.start(current_path=raw)

    _write_append(raw, b"Welcome to Claude Code\n")
    n1 = _run(a.poll())
    assert n1 > 0

    _write_append(raw, b"\n* Thinking...\n")
    n2 = _run(a.poll())
    assert n2 > 0
    a.stop()

    events = _read_events(ss.analyzer_events_path("sess", base=tmp_path))
    types = {e["t"] for e in events}
    assert "session_boot" in types


# -- rotation ----------------------------------------------------------------

def test_switch_file_preserves_session_offset(tmp_path: Path):
    r1 = tmp_path / "raw-1.log"
    r2 = tmp_path / "raw-2.log"
    r1.write_bytes(b"AAAAAAAA\n")  # 9 bytes
    r2.write_bytes(b"BBBB\n")      # 5 bytes

    a = ShadowAnalyzer("sess", base=tmp_path)
    a.start(current_path=r1)
    _run(a.poll())
    a.switch_file(r2)
    _run(a.poll())
    a.stop()

    events = _read_events(ss.analyzer_events_path("sess", base=tmp_path))
    # session_offset (meta 에 기록됨) 은 9 + 5 = 14 이상
    stops = [e for e in events if e["t"] == "shadow.shadow_stop"]
    assert len(stops) == 1
    assert stops[0]["bytes_read"] == 14
    assert stops[0]["rotations"] == 1


# -- resume ------------------------------------------------------------------

def test_resume_continues_from_prev_offset(tmp_path: Path):
    raw = tmp_path / "raw.log"
    raw.write_bytes(b"Welcome to Claude Code\n")

    a1 = ShadowAnalyzer("sess", base=tmp_path)
    a1.start(current_path=raw)
    _run(a1.poll())
    a1.stop()

    # 2nd run — 같은 파일에 데이터 추가, 재시작
    _write_append(raw, b"Resuming session 42\n")
    a2 = ShadowAnalyzer("sess", base=tmp_path)
    a2.start(current_path=raw)
    _run(a2.poll())
    a2.stop()

    events = _read_events(ss.analyzer_events_path("sess", base=tmp_path))
    types = [e["t"] for e in events]
    # 첫 시작의 session_boot + 재시작 후 session_resume 가 각각 1회
    assert types.count("session_boot") == 1
    assert types.count("session_resume") == 1


def test_resume_resets_when_file_inode_changes(tmp_path: Path):
    raw = tmp_path / "raw.log"
    raw.write_bytes(b"Welcome to Claude Code\n")

    a1 = ShadowAnalyzer("sess", base=tmp_path)
    a1.start(current_path=raw)
    _run(a1.poll())
    a1.stop()

    # 저장된 state 의 inode 를 의도적으로 mismatch 값으로 덮어써서 결정론적으로
    # reset_inode_mismatch 경로를 exercises. 과거엔 `unlink + write` 로 커널
    # inode 할당에 의존했으나, Linux CI 의 tmpfs/ext4 가 방금 해제된 inode 를
    # 재사용해 mismatch 가 발생하지 않는 경우가 있었다 (CI flakiness 원인).
    from bridge.shadow_analyzer import ShadowAnalyzer as _SA
    from tools.state_store import StateStore
    store = StateStore(_SA._state_path("sess", base=tmp_path))
    data = store.load()
    assert data is not None and data["identity"]["inode"] is not None
    data["identity"]["inode"] = (data["identity"]["inode"] or 0) + 999_999
    store.save(data)

    a2 = ShadowAnalyzer("sess", base=tmp_path)
    a2.start(current_path=raw)
    _run(a2.poll())
    a2.stop()

    events = _read_events(ss.analyzer_events_path("sess", base=tmp_path))
    attaches = [e for e in events if e["t"] == "shadow.file_attach"]
    reasons = [e["reason"] for e in attaches]
    assert "reset_inode_mismatch" in reasons


def test_resume_resets_when_file_truncated(tmp_path: Path):
    raw = tmp_path / "raw.log"
    raw.write_bytes(b"Welcome to Claude Code\nmore content\n")

    a1 = ShadowAnalyzer("sess", base=tmp_path)
    a1.start(current_path=raw)
    _run(a1.poll())
    a1.stop()

    # truncate (파일 축소) — inode 유지하지만 size < last_offset
    with raw.open("wb") as fh:
        fh.write(b"x")

    a2 = ShadowAnalyzer("sess", base=tmp_path)
    a2.start(current_path=raw)
    a2.stop()

    events = _read_events(ss.analyzer_events_path("sess", base=tmp_path))
    attaches = [e for e in events if e["t"] == "shadow.file_attach"]
    reasons = [e["reason"] for e in attaches]
    assert "reset_truncated" in reasons


# -- 분리 보장 ---------------------------------------------------------------

def test_does_not_write_to_bridge_events_jsonl(tmp_path: Path):
    """frozen parser 보호: bridge 의 events.jsonl 이름을 건드리지 않는다."""
    raw = tmp_path / "raw.log"
    raw.write_bytes(b"Welcome to Claude Code\n")

    a = ShadowAnalyzer("sess", base=tmp_path)
    a.start(current_path=raw)
    _run(a.poll())
    a.stop()

    # analyzer-events.jsonl 만 존재, events.jsonl 은 없다
    d = ss.session_dir("sess", base=tmp_path)
    assert (d / "analyzer-events.jsonl").exists()
    assert not (d / "events.jsonl").exists()


# -- counter 관측 -----------------------------------------------------------

def test_tokenizer_counter_exposed_in_stop_meta(tmp_path: Path):
    raw = tmp_path / "raw.log"
    # 중간에 끊어진 CSI 를 두 번에 걸쳐 feed → 카운터 1+ 기대
    a = ShadowAnalyzer("sess", base=tmp_path)
    raw.write_bytes(b"\x1b[3")
    a.start(current_path=raw)
    _run(a.poll())
    _write_append(raw, b"1mX\n")
    _run(a.poll())
    a.stop()

    events = _read_events(ss.analyzer_events_path("sess", base=tmp_path))
    stop_meta = next(e for e in events if e["t"] == "shadow.shadow_stop")
    assert stop_meta["incomplete_escape_count"] >= 1
    assert stop_meta["bytes_read"] == len(b"\x1b[3") + len(b"1mX\n")
