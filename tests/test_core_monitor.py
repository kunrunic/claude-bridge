"""
20260417_163701 회귀 방지 — QUEUE-SLIP 상태에서 _flush_completed TypeError.

`detect_slip()` 이 None 아닌 값("idx_past_cc" / "anchor_mismatch") 을 반환할 때
이전 코드가 `dump.event("core", "queue_slip", kind=slip, ...)` 로 positional +
keyword `kind` 충돌을 일으켜 TypeError 를 냈다. monitor 루프가 매 tick 이
에러를 잡아 error_count 를 증가시키고 10회 누적 후 스스로 종료했다.

본 테스트는 slip 이 감지되는 상태에서 _flush_completed 가 예외 없이 끝나고
대기 중인 완료 블록을 정상 처리하는지 확인한다.
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

from bridge import sender as sender_mod  # noqa: E402
from bridge.core import Bridge  # noqa: E402


class _FakeBot:
    async def send_message(self, *a, **kw):
        return None


class _FakeApp:
    bot = _FakeBot()


def _block(sig: str) -> str:
    return f"⏺ {sig}\n  ⎿ detail"


def test_flush_completed_with_idx_past_cc_slip_no_typeerror(monkeypatch):
    """slip='idx_past_cc' 상태에서 _flush_completed 가 TypeError 없이 끝난다.

    재현: queue.idx 를 5 로 올려둔 뒤 pane 에 ⏺ 블록이 없는 상태 → detect_slip 은
    "idx_past_cc" 반환. 이전 코드라면 dump.event() 에서 positional+keyword `kind`
    충돌로 TypeError.
    """
    sent_blocks = []

    async def fake_send(app, chat_id, text):
        sent_blocks.append(text)

    monkeypatch.setattr(sender_mod, "_send_output", fake_send)

    br = Bridge()
    br.queue.advance(5)  # idx=5 > len(completed)=0
    br.queue.mark_sent(_block("old_anchor"))

    async def run():
        return await br._flush_completed(
            _FakeApp(), chat_id=0,
            clean="",
            include_last=True, log_tag="test-slip",
        )

    sent = asyncio.run(run())
    assert sent == 0
    assert sent_blocks == []


def test_flush_completed_with_anchor_mismatch_slip_no_typeerror(monkeypatch):
    """slip='anchor_mismatch' 상태에서도 TypeError 없이 복원 경로가 동작한다.

    A evicted, G 새로 추가된 pane → detect_slip="anchor_mismatch" →
    take_new 가 앵커 재탐색으로 G 만 반환 → 정상 전송.
    """
    sent_blocks = []

    async def fake_send(app, chat_id, text):
        sent_blocks.append(text)

    monkeypatch.setattr(sender_mod, "_send_output", fake_send)

    br = Bridge()
    F = _block("F")
    G = _block("G")
    br.queue.advance(6)
    br.queue.mark_sent(F)

    # pane text: B,C,D,E,F,G 완료 (A evicted, G 새로 추가)
    clean = "\n\n".join(_block(x) for x in ["B", "C", "D", "E", "F", "G"])

    async def run():
        return await br._flush_completed(
            _FakeApp(), chat_id=0,
            clean=clean,
            include_last=True, log_tag="test-mismatch",
        )

    sent = asyncio.run(run())
    assert sent == 1
    assert sent_blocks == [G]
