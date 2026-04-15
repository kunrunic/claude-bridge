"""
CB1/CB2 컨텍스트 압축 버그 회귀 방지 테스트 (P0/P1/P2).

- P0: busy가 BUSY_TIMEOUT_SEC 이상 지속되면 watchdog 발동
- P1: `Compacting conversation` / `Crunched for N` 흔적을 감지
- P2: pane 내용이 BUSY_STUCK_SEC 이상 변하지 않으면 stuck 판정
- CB2: `Context limit reached` 감지

bot.py 는 telegram 의존성을 가지므로 import stub 후 로드.
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


def _load_bot():
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))

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
        cfg.write_text('{"token":"x","claude_path":"claude","allowed_ids":[1],"tmux_session":"t"}')

    import importlib
    if "bot" in sys.modules:
        del sys.modules["bot"]
    return importlib.import_module("bot")


bot = _load_bot()


# ---------- P1: 압축 감지 ----------

def test_has_compaction_detects_compacting_line():
    pane = (
        "⏺ 작업 정리했습니다.\n"
        "\n"
        "✻ Compacting conversation...\n"
        "\n"
        "❯ \n"
    )
    assert bot.has_compaction(pane)


def test_has_compaction_detects_crunched_summary():
    pane = (
        "⏺ 분석 완료.\n"
        "\n"
        "✻ Crunched for 2m 39s\n"
        "\n"
        "❯ \n"
    )
    assert bot.has_compaction(pane)


def test_has_compaction_ignores_old_scrolled_out():
    # 40줄 스캔 범위 밖이면 감지 안 됨 (오래된 압축 흔적)
    pane = "✻ Crunched for 1m 00s\n" + "\n".join(["노이즈"] * 100) + "\n❯ \n"
    assert not bot.has_compaction(pane)


def test_has_context_limit():
    pane = "⎿  Context limit reached · /compact or /clear to continue\n❯ \n"
    assert bot.has_context_limit(pane)


def test_has_context_limit_negative():
    assert not bot.has_context_limit("❯ 일반 입력\n")


# ---------- 상수 sanity ----------

def test_watchdog_constants_sane():
    # 타임아웃이 stuck 임계값보다 크거나 같아야 (엣지케이스 중복 방지)
    assert bot.BUSY_TIMEOUT_SEC >= bot.BUSY_STUCK_SEC
    assert bot.BUSY_STUCK_SEC >= 60  # 1분 이하로 내려가면 오탐 가능성


# ---------- P0/P2: watchdog 동작 ----------

class _FakeBot:
    def __init__(self):
        self.sent = []
        self.deleted = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        msg = types.SimpleNamespace(message_id=len(self.sent))
        return msg

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class _FakeApp:
    def __init__(self):
        self.bot = _FakeBot()


def test_watchdog_recovers_pending_response():
    """pane에 미전송 ⏺ 블록이 있으면 복구 전송해야 한다."""
    app = _FakeApp()
    br = bot.Bridge()
    br.status_msg_id = 123
    br.was_busy = True
    br.busy_started_at = 0.0
    br.busy_pane_hash = "x"
    br.busy_last_change_at = 0.0
    br.saw_compaction = False
    br._last_status_text = "⏳ 작업 중… (900s)"

    clean = (
        "❯ 분석해줘\n"
        "\n"
        "⏺ 분석 결과입니다\n"
        "  상세 내용 ...\n"
        "\n"
        "❯ \n"
    )

    asyncio.run(br._busy_watchdog(app, 42, clean, reason="test-timeout"))

    # 상태 메시지 삭제됨
    assert (42, 123) in app.bot.deleted
    # 경고 메시지 + 응답 본문 전송됨
    kinds = [t for _, t in app.bot.sent]
    assert any("test-timeout" in t for t in kinds)
    assert any("⏺" in t for t in kinds)
    # state 초기화
    assert br.was_busy is False
    assert br.status_msg_id is None
    assert br.busy_started_at is None
    assert br.saw_compaction is False


def test_watchdog_notifies_when_no_response():
    """pane에 복구할 ⏺ 블록이 없으면 재전송 안내."""
    app = _FakeApp()
    br = bot.Bridge()
    br.status_msg_id = 55
    br.was_busy = True
    br.busy_started_at = 0.0
    br.busy_pane_hash = "x"
    br.busy_last_change_at = 0.0
    br.saw_compaction = False

    clean = "❯ \n"  # 응답 없음

    asyncio.run(br._busy_watchdog(app, 42, clean, reason="stuck"))

    assert (42, 55) in app.bot.deleted
    notes = [t for _, t in app.bot.sent]
    assert any("다시 보내" in t for t in notes)
    assert br.was_busy is False
