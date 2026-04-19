"""ShadowAnalyzer 가 bridge monitor 에 올바르게 연결됐는지 검증.

실제 tmux/pipe-pane 없이 lifecycle hook 만 확인:
  - BRIDGE_SHADOW_ANALYZER=0 (기본): 모든 shadow hook 이 no-op.
  - BRIDGE_SHADOW_ANALYZER=1: _shadow_start 가 인스턴스 생성, stop 이 해제,
    switch 가 delegate, poll 이 await 됨.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bridge import config
from bridge.core import Bridge


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_shadow_disabled_hooks_are_noop(monkeypatch):
    monkeypatch.setattr(config, "BRIDGE_SHADOW_ANALYZER", False)
    b = Bridge()
    b._shadow_start(Path("/tmp/ignored"))
    assert b._shadow is None
    b._shadow_stop()  # no error
    b._shadow_switch(Path("/tmp/other"))  # no error
    _run(b._shadow_poll())  # no error
    assert b._shadow is None


def test_shadow_enabled_constructs_analyzer(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BRIDGE_SHADOW_ANALYZER", True)
    created: list[MagicMock] = []

    class _FakeAnalyzer:
        def __init__(self, *args, **kwargs):
            self.started_with: Path | None = None
            self.stopped = False
            self.switched_to: Path | None = None
            self.poll_count = 0
            created.append(self)

        def start(self, *, current_path):
            self.started_with = current_path

        def stop(self):
            self.stopped = True

        def switch_file(self, new_path):
            self.switched_to = new_path

        async def poll(self):
            self.poll_count += 1
            return 0

    monkeypatch.setattr("bridge.core.ShadowAnalyzer", _FakeAnalyzer)

    b = Bridge()
    raw = tmp_path / "raw.log"
    b._shadow_start(raw)

    assert len(created) == 1
    a = created[0]
    assert b._shadow is a
    assert a.started_with == raw

    new_raw = tmp_path / "raw2.log"
    b._shadow_switch(new_raw)
    assert a.switched_to == new_raw

    _run(b._shadow_poll())
    assert a.poll_count == 1

    b._shadow_stop()
    assert a.stopped is True
    assert b._shadow is None


def test_shadow_start_exception_does_not_crash_bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BRIDGE_SHADOW_ANALYZER", True)

    class _BoomAnalyzer:
        def __init__(self, *a, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr("bridge.core.ShadowAnalyzer", _BoomAnalyzer)
    b = Bridge()
    b._shadow_start(tmp_path / "raw.log")
    # 에러를 삼키고 _shadow 는 None 유지
    assert b._shadow is None


def test_shadow_poll_exception_does_not_crash_bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BRIDGE_SHADOW_ANALYZER", True)

    class _PollBoom:
        def __init__(self, *a, **kw): pass
        def start(self, *, current_path): pass
        async def poll(self):
            raise RuntimeError("poll boom")
        def stop(self): pass

    monkeypatch.setattr("bridge.core.ShadowAnalyzer", _PollBoom)
    b = Bridge()
    b._shadow_start(tmp_path / "raw.log")
    _run(b._shadow_poll())  # 예외 silent
