"""
20260419_172800 회귀 방지 — Edit/Write 등 "Do you want to <verb> …?" 다이얼로그 미검출.

문제: Claude Code 가 Edit 도구 승인을 요청할 때 pane 에 다음과 같이 그린다.

    Do you want to make this edit to reviewer.py?
     ❯ 1. Yes
       2. Yes, allow all edits during this session (shift+tab)
       3. No

기존 APPROVAL_RE 는 `"Do you want to proceed"` / `"Allow … to"` / `"Proceed?"` /
`"(Y/n)"` / `"(y/N)"` 만 매치 → 이 문구는 하나도 해당 안 됨. `is_approval()` 가
False 를 반환해 `_send_approval` 미호출 → Telegram 에 승인 요청 안 가고 봇 stuck.

상세 재현 / dump / raw log:
  docs/plans/20260419-pipe-pane-redesign-poc/baseline/case-04-edit-approval-wording/

해결: APPROVAL_RE 를 `"Do you want to\\b"` broad 로 넓힘. 3단 검증 (❯ 1. Yes 패턴 +
위 15줄 + Yes 아래 divider 부재) 이 이미 strict 라 broad 매치 안전. 같은 파서 내부
`_approval_context` / `_approval_box` 는 이미 broad 를 쓰던 일관성 균열도 해소.
"""
from __future__ import annotations

from bridge import parser


EDIT_APPROVAL_PANE = """\
⏺ 커밋 완료 (0cc8aa7). 변경 내용 확인.

  257 +    pending_entries: list[dict] = []
  258 +    skipped_entries: list[dict] = []
  259      total = 0
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to make this edit to reviewer.py?
 ❯ 1. Yes
   2. Yes, allow all edits during this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
"""


WRITE_APPROVAL_PANE = """\
⏺ 새 파일 생성 계획:

  - tools/pipe_pane_analyzer.py

────────────────────────────────────────────────────────────────────────────────
 Do you want to create tools/pipe_pane_analyzer.py?
 ❯ 1. Yes
   2. Yes, allow all writes during this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
"""


BASH_APPROVAL_PANE = """\
⏺ Bash 실행 요청:

  find . -name "*.log" -delete

────────────────────────────────────────────────────────────────────────────────
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for rm commands in ~/claude-bridge2
   3. No

 Esc to cancel · Tab to amend
"""


def test_is_approval_accepts_edit_dialog():
    """case-04: `Do you want to make this edit to <file>?` 도 승인으로 인식해야."""
    assert parser.is_approval(EDIT_APPROVAL_PANE) is True


def test_is_approval_accepts_write_dialog():
    """Write/Create 변형 `Do you want to create <file>?` 도 인식."""
    assert parser.is_approval(WRITE_APPROVAL_PANE) is True


def test_is_approval_still_accepts_bash_proceed_dialog():
    """기존 Bash 계열 `Do you want to proceed?` 는 그대로 동작해야 (회귀 방지)."""
    assert parser.is_approval(BASH_APPROVAL_PANE) is True


def test_is_approval_rejects_edit_echo_with_divider_below():
    """Edit 다이얼로그 텍스트가 scrollback 에 echo 된 경우 — Yes 아래 divider 있으면 False."""
    echoed = EDIT_APPROVAL_PANE + """\

────────────────────────────────────────────────────────────────────────────────
  ❯
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""
    assert parser.is_approval(echoed) is False
