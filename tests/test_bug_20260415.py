"""
20260415_150241 버그 회귀 방지 테스트.

- A: 부팅 시점 stale ⏺ 가 다시 전송되지 않도록 monitor() 가 _sent_keys 시드.
- B: _approval_box() 가 '봇방 설명' 같은 위쪽 ⏺ 본문을 승인 카드에 끌어오지 않음.

bot.py 는 telegram 패키지에 의존하므로 import 를 stub 처리한 뒤 로드한다.
"""
from __future__ import annotations

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
        cfg.write_text('{"token":"x","claude_path":"/bin/true","allowed_ids":[1],"tmux_session":"t"}')

    import importlib
    if "bot" in sys.modules:
        del sys.modules["bot"]
    return importlib.import_module("bot")


bot = _load_bot()


# ---------- B: _approval_box ----------

APPROVAL_PANE = """\
⏺ 봇방은 1:1 개인 대화라 "나가기"가 없습니다.

  ┌────────────────┬───────────┬────────────┐
  │                │ 그룹 채팅 │ 봇방 (1:1) │
  └────────────────┴───────────┴────────────┘

❯ [텔레그램 이미지 첨부]

⏺ Reading 1 file…
  ⎿  ~/claude-bridge2/tg_images/tg_1776231630.jpg

────────────────────────────────────────────────────────────────────────────────
 Read file

  Read(~/claude-bridge2/tg_images/tg_1776231630.jpg)

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, allow reading from tg_images/ during this session
   3. No

 Esc to cancel · Tab to amend
"""


def test_approval_box_excludes_prior_response():
    box = bot._approval_box(APPROVAL_PANE)
    assert "Do you want to proceed" in box
    assert "Read file" in box
    # 위쪽 ⏺ 응답(봇방 설명/표) 본문이 카드에 끌려오지 않아야 함
    assert "나가기" not in box
    assert "그룹 채팅" not in box
    assert "Reading 1 file" not in box


def test_approval_box_fallback_when_no_prompt():
    box = bot._approval_box("nothing here\nshort line")
    assert box  # 빈문자열 아님 (tail fallback)


# ---------- A: monitor 부팅 시드 ----------

def test_extract_last_response_picks_last_block():
    """시드 로직이 의존하는 extract_last_response 의 동작 보증."""
    pane = APPROVAL_PANE  # 마지막 ⏺ 는 Reading 1 file… 블록
    last = bot.extract_last_response(pane)
    assert last.startswith("⏺")
    assert "Reading 1 file" in last


def test_mark_sent_dedup_via_response_key():
    """부팅 시드 후 같은 응답이 다시 들어와도 _already_sent 가 True."""
    b = bot.Bridge()
    seed = bot.extract_last_response(APPROVAL_PANE)
    assert seed
    b._mark_sent(seed)
    # 공백/줄바꿈만 다른 동일 본문도 중복으로 잡혀야 함
    noisy = "\n\n" + seed + "   \n"
    assert b._already_sent(noisy)
