"""
20260417_164104 회귀 방지 — `is_approval` 이 scrollback echo 를 라이브 승인창으로 오인.

문제: 사용자가 텔레그램에 approval 박스 텍스트를 붙여넣으면 bridge 가 그것을
pane 에 literal paste 로 echo 하고, 다음 monitor tick 에서 `is_approval(text) == True`
가 되어 pre-approval → ✅ 승인 루프가 반복 발사됐다.

해결: 라이브 승인창은 입력 박스 자리를 차지해서 `❯ 1. Yes` **아래에 전폭 ─ divider
가 없다**. Echo 는 Claude Code 가 자기 입력 박스를 계속 렌더하므로 Yes 아래에
divider 가 반드시 존재한다. 이 구조적 차이를 가드로 추가.
"""
from __future__ import annotations

from bridge import parser


ECHOED_PANE = """⏺ 이전 질문에 대한 답:
  (어쩌구저쩌구)
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, allow all edits during this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
────────────────────────────────────────────────────────────────────────────────
  ❯
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle)              ◉ xhigh · /effort
"""


LIVE_APPROVAL_PANE = """\
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


COMPACT_INLINE_PANE = "Bash(ls -la)\n─────────\nDo you want to proceed?\n❯ 1. Yes  2. No"


def test_is_approval_rejects_scrollback_echo():
    """사용자가 텔레그램에 붙여넣은 approval 텍스트가 pane 에 echo 된 케이스.
    Yes 줄 아래에 전폭 ─ divider(입력 박스) 가 남아 있으므로 False 여야 한다."""
    assert parser.is_approval(ECHOED_PANE) is False


def test_is_approval_accepts_live_box():
    """라이브 승인창: Yes 아래에 divider 없음 → True."""
    assert parser.is_approval(LIVE_APPROVAL_PANE) is True


def test_is_approval_accepts_compact_box_inline():
    """`test_approval_reconnect.py` 가 쓰는 컴팩트 inline 포맷: True."""
    assert parser.is_approval(COMPACT_INLINE_PANE) is True


def test_is_approval_rejects_text_without_yes_choice():
    """프롬프트만 있고 `❯ 1. Yes` 가 없는 텍스트는 False."""
    assert parser.is_approval("Do you want to proceed?\nsome other text\n") is False


def test_is_approval_rejects_yes_without_prompt():
    """`❯ 1. Yes` 만 있고 근처에 `Do you want to …` 가 없으면 False."""
    assert parser.is_approval("random chatter\n" * 20 + "❯ 1. Yes\n") is False


if __name__ == "__main__":
    import sys
    import traceback
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if failures else 0)
