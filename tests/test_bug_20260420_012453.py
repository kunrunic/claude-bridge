"""
20260420_012453 회귀 방지 — 승인 직후 busy 전환 중 is_approval 오탐 → ghost approval wedge.

문제: 사용자가 승인 Yes 를 눌러 tmux 로 Enter 를 송신한 직후, Claude 가 tool 실행에
진입하면 pane 에 ``esc to interrupt`` 가 등장하는 busy 상태로 전환된다. 이때 tail 60 줄
안에는 방금 사라진 승인 박스의 ``❯ 1. Yes`` / ``Do you want to …`` 문자열이
scrollback 잔재로 남아 있는 좁은 창이 존재한다. 기존 ``is_approval()`` 의 3단 검증은
이 잔재를 라이브 승인으로 오인해 True 를 리턴 → core loop 가 두 번째 ``send_approval``
을 발화하고 ``awaiting_approval=True`` 로 다시 잠긴다. 이후 모든 응답 포워딩이
``awaiting_approval`` 가드에 막혀 wedge.

재현 환경 (실측):
    dump/20260420/012453_claude-bridge2/events.jsonl
      01:26:36.367 receiver.callback(approve_yes)
      01:26:36.384 tmux.send_key(Enter)
      01:26:36.400 sender.send_approval  ← ghost duplicate
      (이후 pane_tick 은 모두 awaiting_approval=True 로 wedge)

해결: ``is_approval()`` 맨 앞에 ``is_busy(text)`` 게이트 추가 — busy(tool 실행중) 와
라이브 승인 박스는 상호 배타이므로 busy 면 False 를 반환.
"""
from __future__ import annotations

from bridge import parser


# 승인 직후, tool 실행이 시작되어 상태바가 `esc to interrupt` 로 바뀌었지만
# scrollback 에는 방금 닫힌 승인 박스 본문이 아직 남아있는 상태.
# 핵심: Yes 줄 아래에 solid `─` divider 가 **없음** (╌ dashed divider 와 busy
# 상태바만 있음) → is_approval 의 기존 3단 체크는 통과 → 구 로직은 True 반환.
# is_busy 는 BUSY_CHECK_TAIL 꼬리에서 `esc to interrupt` 를 찾아 True.
POST_APPROVAL_BUSY_PANE = """\
⏺ 파일을 수정합니다.

  257 +    pending_entries: list[dict] = []
  258 +    skipped_entries: list[dict] = []
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to make this edit to parser.py?
 ❯ 1. Yes
   2. Yes, allow all edits during this session (shift+tab)
   3. No

✻ Cogitating… (3s · esc to interrupt)
"""


# 대조군: busy 가 아닌 라이브 승인 박스 — 반드시 True 여야 함.
LIVE_APPROVAL_PANE = """\
⏺ 파일을 수정합니다.

  257 +    pending_entries: list[dict] = []
  258 +    skipped_entries: list[dict] = []
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to make this edit to parser.py?
 ❯ 1. Yes
   2. Yes, allow all edits during this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
"""


def test_is_approval_rejects_busy_scrollback():
    """busy (`esc to interrupt`) 중이면 tail 에 `❯ 1. Yes` 가 남아있어도 승인 아님."""
    assert parser.is_busy(POST_APPROVAL_BUSY_PANE), "precondition: pane must be busy"
    assert parser.is_approval(POST_APPROVAL_BUSY_PANE) is False, (
        "busy 중엔 `❯ 1. Yes` 는 scrollback 잔재 — ghost approval 방지"
    )


def test_is_approval_still_accepts_idle_live_box():
    """대조군: busy 가 아닌 라이브 승인 박스는 여전히 True."""
    assert parser.is_busy(LIVE_APPROVAL_PANE) is False
    assert parser.is_approval(LIVE_APPROVAL_PANE) is True
