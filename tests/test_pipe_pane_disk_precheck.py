"""§13.4 Pre-flight disk check — P0 offset 무결성 safeguard.

BRIDGE_PIPE_PANE_DIR 의 가용 공간이 BRIDGE_PIPE_PANE_MIN_FREE_MB 미만이면
monitor 진입 시 raw 로그 수집을 **건너뛴다**. pipe-pane 이 가득 찬 디스크에서
조용히 죽어 오프셋 무결성이 깨지는 시나리오 방지.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch


def _stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _ensure_bridge_importable():
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))

    if "telegram" in sys.modules:
        return

    class _DummyMeta(type):
        def __getattr__(cls, _): return cls

    class _Dummy(metaclass=_DummyMeta):
        def __init__(self, *a, **kw): pass
        def __call__(self, *a, **kw): return self
        def __getattr__(self, _): return self

    tg = _stub("telegram")
    tg.Update = _Dummy
    tg.InlineKeyboardButton = _Dummy
    tg.InlineKeyboardMarkup = _Dummy
    tg.BotCommand = _Dummy
    _stub("telegram.ext",
          Application=_Dummy, ApplicationBuilder=_Dummy,
          CommandHandler=_Dummy, MessageHandler=_Dummy,
          CallbackQueryHandler=_Dummy, ContextTypes=_Dummy, filters=_Dummy())
    _stub("telegram.request", HTTPXRequest=_Dummy)

    cfg = root / "config.json"
    if not cfg.exists():
        import json
        cfg.write_text(json.dumps({
            "token": "x", "allowed_ids": [1],
            "tmux_session": "claude_bridge_test",
            "claude_path": "/usr/bin/claude",
        }))


_ensure_bridge_importable()

from bridge import config, core, tmux as tmux_mod  # noqa: E402


class _FakeUsage:
    def __init__(self, free_bytes: int):
        self.total = 1_000_000_000
        self.used = self.total - free_bytes
        self.free = free_bytes


def _new_bridge() -> core.Bridge:
    return core.Bridge()


# -- _disk_precheck 단위 --------------------------------------------------

def test_disk_precheck_passes_when_free_space_sufficient():
    b = _new_bridge()
    free_bytes = (config.BRIDGE_PIPE_PANE_MIN_FREE_MB + 10) * 1024 * 1024
    with patch("bridge.core.shutil.disk_usage", return_value=_FakeUsage(free_bytes)):
        ok, free_mb = b._disk_precheck()
    assert ok is True
    assert free_mb >= config.BRIDGE_PIPE_PANE_MIN_FREE_MB


def test_disk_precheck_fails_when_under_threshold():
    b = _new_bridge()
    free_bytes = 100 * 1024 * 1024  # 100MB << 500MB 기본
    with patch("bridge.core.shutil.disk_usage", return_value=_FakeUsage(free_bytes)):
        ok, free_mb = b._disk_precheck()
    assert ok is False
    assert free_mb == 100


def test_disk_precheck_lenient_on_stat_error():
    """stat 실패 (권한/미존재) 는 best-effort 통과. OSError 로 사용자 기능을 막지 않는다."""
    b = _new_bridge()
    with patch("bridge.core.shutil.disk_usage", side_effect=OSError("boom")):
        ok, free_mb = b._disk_precheck()
    assert ok is True
    assert free_mb == -1


# -- _pipe_attach 통합 ----------------------------------------------------

def test_pipe_attach_skips_start_when_disk_low(monkeypatch):
    b = _new_bridge()
    # start_pipe_pane 이 호출되면 안 된다.
    called = {"start": 0}

    async def fake_start(path: str, target=None):
        called["start"] += 1
        return True

    monkeypatch.setattr(tmux_mod, "start_pipe_pane", fake_start)
    monkeypatch.setattr(config, "BRIDGE_PIPE_PANE_ENABLED", True)

    low_free = 10 * 1024 * 1024  # 10MB
    with patch("bridge.core.shutil.disk_usage", return_value=_FakeUsage(low_free)):
        asyncio.run(b._pipe_attach())

    assert called["start"] == 0
    assert b._pipe_log_path is None


def test_pipe_attach_proceeds_when_disk_ok(monkeypatch):
    b = _new_bridge()
    paths: list[str] = []

    async def fake_start(path: str, target=None):
        paths.append(path)
        return True

    monkeypatch.setattr(tmux_mod, "start_pipe_pane", fake_start)
    monkeypatch.setattr(config, "BRIDGE_PIPE_PANE_ENABLED", True)

    high_free = (config.BRIDGE_PIPE_PANE_MIN_FREE_MB + 100) * 1024 * 1024
    with patch("bridge.core.shutil.disk_usage", return_value=_FakeUsage(high_free)):
        asyncio.run(b._pipe_attach())

    assert len(paths) == 1
    assert b._pipe_log_path is not None
    assert str(b._pipe_log_path) == paths[0]


def test_pipe_attach_emits_dump_event_on_disk_fail(monkeypatch):
    from bridge import dump as dump_mod

    b = _new_bridge()
    events: list[tuple] = []

    def fake_event(source, kind, **kw):
        events.append((source, kind, kw))

    async def fake_start(path: str, target=None):
        return True

    monkeypatch.setattr(tmux_mod, "start_pipe_pane", fake_start)
    monkeypatch.setattr(dump_mod, "event", fake_event)
    monkeypatch.setattr(config, "BRIDGE_PIPE_PANE_ENABLED", True)

    low_free = 50 * 1024 * 1024
    with patch("bridge.core.shutil.disk_usage", return_value=_FakeUsage(low_free)):
        asyncio.run(b._pipe_attach())

    fail_events = [e for e in events if e[1] == "pipe_pane_disk_precheck_fail"]
    assert len(fail_events) == 1
    _, _, payload = fail_events[0]
    assert payload["free_mb"] == 50
    assert payload["min_free_mb"] == config.BRIDGE_PIPE_PANE_MIN_FREE_MB


# -- _pipe_maybe_rotate -----------------------------------------------------

def test_pipe_maybe_rotate_skips_on_low_disk(monkeypatch, tmp_path):
    b = _new_bridge()
    # 기존 로그 파일이 MAX 를 초과한 크기로 존재. sparse file (truncate) 로
    # 실디스크 소비 없이 큰 size 를 만든다.
    dummy = tmp_path / "raw-x.log"
    with dummy.open("wb") as fh:
        fh.truncate(config.BRIDGE_PIPE_PANE_MAX_BYTES + 1)
    b._pipe_log_path = dummy
    b._pipe_last_size_check = 0  # 즉시 체크 가능

    called = {"start": 0}

    async def fake_start(path, target=None):
        called["start"] += 1
        return True

    monkeypatch.setattr(tmux_mod, "start_pipe_pane", fake_start)
    monkeypatch.setattr(config, "BRIDGE_PIPE_PANE_ENABLED", True)

    low_free = 50 * 1024 * 1024
    with patch("bridge.core.shutil.disk_usage", return_value=_FakeUsage(low_free)):
        asyncio.run(b._pipe_maybe_rotate())

    assert called["start"] == 0, "disk 부족 시 rotate 가 새 pipe 를 띄우면 안 된다"
    assert b._pipe_log_path == dummy, "현재 파일은 계속 참조"
