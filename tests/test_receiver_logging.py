"""
20260417_163701 회귀 방지 — 슬래시 명령 로그 보강.

CB2 tmux 세션이 조용히 사라진 사건의 추적성을 위해 cmd_* 함수들이
`[USER→BOT] /<명령>` 로그를 남기도록 수정. 이 테스트는 추후 같은 로그가
실수로 제거되지 않도록 회귀 방지.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path


def _stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _prep():
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

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    cfg = root / "config.json"
    if not cfg.exists():
        cfg.write_text(
            '{"token":"x","claude_path":"claude","allowed_ids":[1],'
            '"tmux_session":"claude_bridge_test"}'
        )


_prep()

from bridge import config as config_mod  # noqa: E402
from bridge import receiver as receiver_mod  # noqa: E402


class _FakeMsg:
    async def reply_text(self, *a, **kw):
        pass

    async def set_reaction(self, *a, **kw):
        pass


class _FakeChat:
    id = 1


class _FakeUpdate:
    def __init__(self):
        self.message = _FakeMsg()
        self.effective_chat = _FakeChat()
        self.effective_message = self.message


def _capture_logs(monkeypatch):
    logs: list[tuple[str, str]] = []

    def spy(tag, msg=""):
        logs.append((tag, msg))

    # receiver.py 는 `from .config import _log` 로 로컬 레퍼런스를 가져오므로
    # 양쪽 모두 교체해야 한다.
    monkeypatch.setattr(receiver_mod, "_log", spy)
    monkeypatch.setattr(config_mod, "_log", spy)
    return logs


def test_cmd_end_logs_user_bot(monkeypatch):
    """/end 가 [USER→BOT] /end 로그를 남긴다 (CB2 tmux 사망 추적성)."""
    monkeypatch.setattr(config_mod, "ALLOWED_IDS", {1})
    logs = _capture_logs(monkeypatch)

    async def noop_stop():
        return None

    monkeypatch.setattr(receiver_mod.bridge, "stop", noop_stop)

    asyncio.run(receiver_mod.cmd_end(_FakeUpdate(), None))

    assert ("USER→BOT", "/end") in logs


def test_cmd_start_logs_user_bot(monkeypatch):
    monkeypatch.setattr(config_mod, "ALLOWED_IDS", {1})
    logs = _capture_logs(monkeypatch)

    # has-session 은 세션 없음(returncode=1) 으로 응답 → 세션 목록 분기
    class _R:
        returncode = 1
    monkeypatch.setattr(receiver_mod.tmux, "tmux_run", lambda *a, **kw: _R())
    monkeypatch.setattr(receiver_mod.session, "find_sessions", lambda: [])

    asyncio.run(receiver_mod.cmd_start(_FakeUpdate(), None))

    assert ("USER→BOT", "/start") in logs


def test_cmd_esc_logs_user_bot(monkeypatch):
    monkeypatch.setattr(config_mod, "ALLOWED_IDS", {1})
    logs = _capture_logs(monkeypatch)

    class _R:
        returncode = 1  # 세션 없음으로 조기 리턴
    monkeypatch.setattr(receiver_mod.tmux, "tmux_run", lambda *a, **kw: _R())

    asyncio.run(receiver_mod.cmd_esc(_FakeUpdate(), None))

    assert ("USER→BOT", "/esc") in logs


def test_cmd_unlock_logs_user_bot(monkeypatch):
    monkeypatch.setattr(config_mod, "ALLOWED_IDS", {1})
    logs = _capture_logs(monkeypatch)

    monkeypatch.setattr(receiver_mod.session, "_my_locks", lambda: [])

    asyncio.run(receiver_mod.cmd_unlock(_FakeUpdate(), None))

    assert ("USER→BOT", "/unlock") in logs


def test_cmd_model_logs_user_bot(monkeypatch):
    monkeypatch.setattr(config_mod, "ALLOWED_IDS", {1})
    logs = _capture_logs(monkeypatch)

    class _R:
        returncode = 1  # 세션 없음으로 조기 리턴
    monkeypatch.setattr(receiver_mod.tmux, "tmux_run", lambda *a, **kw: _R())

    asyncio.run(receiver_mod.cmd_model(_FakeUpdate(), None))

    assert ("USER→BOT", "/model") in logs
