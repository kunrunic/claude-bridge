"""20260418_094744 회귀 방지 — /esc 로 승인 모달 취소 후 응답 영구 누락.

현상: 사용자가 승인 모달을 `/esc` 로 닫으면, 그 이후 Claude 가 보내는 모든
응답이 텔레그램으로 전달되지 않음 (6 시간+, 메시지 4 건 누적). tmux pane 에는
응답이 찍혀 있지만 `[AI→BOT] response` / `[BOT→USER] delivered` 로그 0 건.

원인 두 가지가 연쇄:
  (A) 주: parser._response_region 이 scrollback 에 잔존한 "Do you want to
      proceed?" 텍스트를 라이브 모달로 오인해 응답 영역 end 를 모달 앞
      divider 로 영구 고정 → extract_response_blocks 가 항상 빈 리스트.
  (B) 부: receiver.cmd_esc 가 bridge.awaiting_approval 을 복원하지 않아
      monitor 의 sleep 분기에 갇힘 → ESC 직후 Claude 응답을 못 봄.

해결:
  - Fix 1 (parser): 승인 경계 truncation 을 is_approval(text) 로 게이트,
    매칭 위치도 tail 60 줄 안으로 제한.
  - Fix 2 (receiver): cmd_esc 에서 bridge.awaiting_approval=False 로 복원.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest


# -- Fix 1 회귀 테스트 (parser) ----------------------------------------------

from bridge import parser  # noqa: E402


STALE_APPROVAL_PANE = """\
⏺ Bash(ls)

────────────────────────────────────────────────────────────────
 Bash command
   ls
 Do you want to proceed?
 ❯ 1. Yes
   2. No
 Esc to cancel · Tab to amend · ctrl+e to explain
────────────────────────────────────────────────────────────────
❯ 여기요?

⏺ 네, 여기 있습니다.

────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────
"""


def test_response_region_ignores_stale_approval_after_esc():
    """ESC 로 닫힌 모달의 scrollback 잔존 텍스트가 응답 영역을 잘라먹지 않는다.

    pane 아래에 살아있는 입력 박스 divider 가 있으므로 is_approval 은 False →
    truncation 스킵 → ESC 직후 찍힌 ⏺ 블록이 보존돼야 한다.
    """
    # 사전 확인: is_approval 이 ESC 로 닫힌 모달을 라이브로 착각하면 안 됨
    assert parser.is_approval(STALE_APPROVAL_PANE) is False, (
        "stale approval text 가 is_approval=True 로 오인되면 본 테스트의 전제가 무너진다"
    )

    blocks = parser.extract_response_blocks(STALE_APPROVAL_PANE)
    assert any("네, 여기 있습니다." in b for b in blocks), (
        f"ESC 후 응답이 누락됐다: blocks={blocks!r}"
    )


def test_response_region_still_truncates_on_live_approval():
    """behavior pin: 라이브 승인 모달일 때는 여전히 end 를 divider 로 자른다.

    is_approval=True 이고 tail 60 줄 안에 프롬프트가 있으면 Fix 1 의 gate 를
    통과해 원래 truncation 동작이 수행돼야 한다.
    """
    live_approval_pane = "\n".join([
        "⏺ Bash(ls)",
        "",
        "─" * 64,
        " Bash command",
        "   ls",
        " Do you want to proceed?",
        " ❯ 1. Yes",
        "   2. No",
        " Esc to cancel · Tab to amend",
    ])
    # 라이브 모달 판정 확인
    assert parser.is_approval(live_approval_pane) is True
    lines, end = parser._response_region(live_approval_pane)
    # divider 위치(인덱스 2)에서 end 가 잘려야 함
    assert end <= 2, f"live approval 경계 truncation 이 동작하지 않음: end={end}"


# -- Fix 2 회귀 테스트 (receiver) --------------------------------------------

def _stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _prep_receiver_imports():
    """test_receiver_logging.py 와 동일한 telegram 스텁 설치."""
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

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    cfg = root / "config.json"
    if not cfg.exists():
        cfg.write_text(
            '{"token":"x","claude_path":"claude","allowed_ids":[1],'
            '"tmux_session":"claude_bridge_test"}'
        )


_prep_receiver_imports()

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


def test_cmd_esc_clears_awaiting_approval(monkeypatch):
    """cmd_esc 가 bridge.awaiting_approval 을 False 로 복원한다."""
    monkeypatch.setattr(config_mod, "ALLOWED_IDS", {1})

    # has-session 은 세션 있음(0) 으로 응답 → 실제 send_key 경로까지 진입
    class _R:
        returncode = 0
    monkeypatch.setattr(receiver_mod.tmux, "tmux_run", lambda *a, **kw: _R())
    monkeypatch.setattr(receiver_mod.tmux, "send_key", lambda *a, **kw: None)

    # 전제: 승인 대기 상태
    receiver_mod.bridge.awaiting_approval = True

    asyncio.run(receiver_mod.cmd_esc(_FakeUpdate(), None))

    assert receiver_mod.bridge.awaiting_approval is False, (
        "cmd_esc 이후 awaiting_approval 이 복원되지 않음 — "
        "monitor 의 sleep 분기에서 post-ESC 응답을 놓친다"
    )


def test_cmd_esc_does_not_touch_queue(monkeypatch):
    """정책 확인: cmd_esc 는 queue.reset() 을 부르지 않는다.

    다음 on_message 가 새 turn 진입 시 자동으로 reset 하므로 여기서 리셋하면
    오히려 ESC 직후 Claude 가 찍은 응답의 queue 커서를 어긋나게 만든다.
    """
    monkeypatch.setattr(config_mod, "ALLOWED_IDS", {1})

    class _R:
        returncode = 0
    monkeypatch.setattr(receiver_mod.tmux, "tmux_run", lambda *a, **kw: _R())
    monkeypatch.setattr(receiver_mod.tmux, "send_key", lambda *a, **kw: None)

    receiver_mod.bridge.awaiting_approval = True
    receiver_mod.bridge.queue.seed(3)
    idx_before = receiver_mod.bridge.queue.idx

    asyncio.run(receiver_mod.cmd_esc(_FakeUpdate(), None))

    assert receiver_mod.bridge.queue.idx == idx_before, (
        "cmd_esc 가 queue.idx 를 건드렸다 — 정책 위반"
    )
