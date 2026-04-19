"""§13.4 — File identity 캡처 + resume 검증.

bridge 측: `pipe_pane_identity` 이벤트 + rotate 시 old/new identity 첨부.
analyzer 측: `tools.file_identity.verify_resume` 로 accept / reset 판정.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest


# -- 공통 setup -----------------------------------------------------------

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

from tools.file_identity import FileIdentity, verify_resume  # noqa: E402


# -- FileIdentity.capture -------------------------------------------------

def test_capture_existing_file(tmp_path: Path):
    p = tmp_path / "raw.log"
    p.write_bytes(b"hello")
    fi = FileIdentity.capture(p)
    assert fi.existed is True
    assert fi.path == str(p)
    assert fi.inode is not None
    assert fi.size == 5
    assert fi.mtime_ns is not None


def test_capture_missing_file(tmp_path: Path):
    fi = FileIdentity.capture(tmp_path / "does-not-exist.log")
    assert fi.existed is False
    assert fi.inode is None


def test_from_event_payload_roundtrip(tmp_path: Path):
    p = tmp_path / "raw.log"
    p.write_bytes(b"abcdef")
    original = FileIdentity.capture(p)
    # dump.event 형식으로 직렬화되어 저장됐다가 재구성되는 시나리오
    payload = {
        "path": original.path,
        "existed": True,
        "inode": original.inode,
        "size": original.size,
        "mtime_ns": original.mtime_ns,
    }
    reconstructed = FileIdentity.from_event_payload(payload)
    assert reconstructed == original


# -- verify_resume --------------------------------------------------------

def _fi(**kw):
    base = dict(path="/tmp/x", existed=True, inode=1, size=100, mtime_ns=0)
    base.update(kw)
    return FileIdentity(**base)


def test_verify_resume_accept_on_match_and_grown():
    expected = _fi(size=100)
    current = _fi(size=200)
    assert verify_resume(expected, current, last_offset=100) == "accept"


def test_verify_resume_accept_when_size_equals_offset():
    expected = _fi(size=100)
    current = _fi(size=100)
    assert verify_resume(expected, current, last_offset=100) == "accept"


def test_verify_resume_reset_on_inode_change():
    expected = _fi(inode=1)
    current = _fi(inode=2)
    assert verify_resume(expected, current, last_offset=50) == "reset_inode_mismatch"


def test_verify_resume_reset_on_truncate():
    expected = _fi(size=200)
    current = _fi(size=50)
    assert verify_resume(expected, current, last_offset=100) == "reset_truncated"


def test_verify_resume_reset_when_file_missing():
    expected = _fi()
    current = FileIdentity(path="/tmp/x", existed=False)
    assert verify_resume(expected, current, last_offset=0) == "reset_missing"


def test_verify_resume_reset_when_identity_incomplete():
    expected = FileIdentity(path="/tmp/x", existed=True)  # inode=None
    current = _fi()
    assert verify_resume(expected, current, last_offset=0) == "reset_missing"


# -- bridge 측 dump 이벤트 통합 ------------------------------------------

from bridge import config, core, dump as dump_mod, tmux as tmux_mod  # noqa: E402


class _FakeUsage:
    total = 10_000_000_000
    used = 0
    free = 10_000_000_000


def _hi_disk_patch():
    return patch("bridge.core.shutil.disk_usage", return_value=_FakeUsage())


@pytest.fixture
def captured_events(monkeypatch):
    """dump.event 를 가로채 리스트로 수집."""
    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        dump_mod, "event",
        lambda source, kind, **kw: events.append((source, kind, kw)),
    )
    return events


def test_pipe_attach_emits_identity_open(captured_events, monkeypatch, tmp_path):
    b = core.Bridge()

    async def fake_start(path: str, target=None):
        Path(path).write_bytes(b"")
        return True

    monkeypatch.setattr(tmux_mod, "start_pipe_pane", fake_start)
    monkeypatch.setattr(config, "BRIDGE_PIPE_PANE_ENABLED", True)
    monkeypatch.setattr(
        b, "_new_pipe_log_path", lambda: tmp_path / "raw-open.log"
    )

    with _hi_disk_patch():
        asyncio.run(b._pipe_attach())

    idents = [e for e in captured_events if e[1] == "pipe_pane_identity"]
    assert len(idents) == 1
    _, _, payload = idents[0]
    assert payload["phase"] == "open"
    assert payload["existed"] is True
    assert payload["inode"] is not None


def test_pipe_detach_emits_identity_close(captured_events, monkeypatch, tmp_path):
    b = core.Bridge()
    log = tmp_path / "raw-close.log"
    log.write_bytes(b"hello world")
    b._pipe_log_path = log

    async def fake_stop(target=None):
        return None

    monkeypatch.setattr(tmux_mod, "stop_pipe_pane", fake_stop)
    monkeypatch.setattr(config, "BRIDGE_PIPE_PANE_ENABLED", True)

    asyncio.run(b._pipe_detach())

    idents = [e for e in captured_events if e[1] == "pipe_pane_identity"]
    assert len(idents) == 1
    _, _, payload = idents[0]
    assert payload["phase"] == "close"
    assert payload["size"] == 11


def test_rotate_event_includes_old_and_new_identity(
    captured_events, monkeypatch, tmp_path
):
    b = core.Bridge()
    old = tmp_path / "raw-old.log"
    with old.open("wb") as fh:
        fh.truncate(config.BRIDGE_PIPE_PANE_MAX_BYTES + 1)
    b._pipe_log_path = old
    b._pipe_last_size_check = 0

    new_target = tmp_path / "raw-new.log"

    async def fake_start(path: str, target=None):
        Path(path).write_bytes(b"")
        return True

    monkeypatch.setattr(tmux_mod, "start_pipe_pane", fake_start)
    monkeypatch.setattr(config, "BRIDGE_PIPE_PANE_ENABLED", True)
    monkeypatch.setattr(b, "_new_pipe_log_path", lambda: new_target)

    with _hi_disk_patch():
        asyncio.run(b._pipe_maybe_rotate())

    rotates = [e for e in captured_events if e[1] == "pipe_pane_rotate"]
    assert len(rotates) == 1
    _, _, payload = rotates[0]
    assert payload["old_identity"]["existed"] is True
    assert payload["old_identity"]["path"] == str(old)
    assert payload["new_identity"]["existed"] is True
    assert payload["new_identity"]["path"] == str(new_target)
    # old vs new inode 은 실제 다른 파일이므로 다름
    assert (
        payload["old_identity"]["inode"] != payload["new_identity"]["inode"]
    )
