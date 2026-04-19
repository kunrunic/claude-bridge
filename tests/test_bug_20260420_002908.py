"""20260420_002908 회귀 방지 — approval 모달의 `╌` (dashed) divider 미인식.

문제: Claude Code 가 Edit/Write 등 승인 박스 상단에 `╌` (U+254C BOX DRAWINGS
LIGHT DOUBLE DASH HORIZONTAL) divider 를 그리는데, 기존 `_response_region`
내부 `is_divider` 와 `_approval_box` 의 divider 인식이 `─` (U+2500 solid) 만
허용해 경계를 찾지 못했다.

결과 증상:
  - `_response_region` end 가 pane 끝까지 내려감 (= approval 모달 body 포함)
  - `extract_response_blocks` 가 approval 의 `❯ 1. Yes` 를 user_prompt 로
    오인해 scan_start 가 그 뒤로 밀림 → 응답 ⏺ 블록 0개 반환
  - pre-approval flush 가 아무것도 보내지 않음
  - 사용자가 승인 버튼 누르면 Enter 가 주입되어 approval 이 닫히고, 다음
    monitor tick 에서야 ⏺ 블록이 정상 추출되어 전송
  → 텔레그램 표시 순서 역전: "승인 카드 → (press) → 실제 응답 텍스트"

수정: `is_divider` 와 `_approval_box` 가 `─` / `╌` 둘 다 인식.
"""
from __future__ import annotations

from bridge import parser


EDIT_APPROVAL_PANE = """\
⏺ 이제 bot.py 수정. skip 핸들러를 실제 MCP 호출로 바꾸고 /skipped admin 명령 추가.

  Read 1 file (ctrl+o to expand)

⏺ Update(~/ai_env/.claude/worktrees/happy-blackwell/mcp/reviewer_bot/bot.py)

  257 +    pending_entries: list[dict] = []
  258 +    skipped_entries: list[dict] = []
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to make this edit to bot.py?
 ❯ 1. Yes
   2. Yes, allow all edits during this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
"""


def test_dashed_divider_bounds_response_region():
    """`╌` divider 가 approval 경계로 인식되어 response region end 가
    divider 위에서 잘려야 한다."""
    lines, end = parser._response_region(EDIT_APPROVAL_PANE)
    assert end < len(lines), "divider 를 못 찾으면 end=len(lines) 가 된다"
    # divider 줄을 찾아 end 가 그 위를 가리키는지 확인
    divider_idx = next(
        i for i, ln in enumerate(lines)
        if set(ln.strip()) <= {"╌"} and len(ln.strip()) > 20
    )
    assert end == divider_idx


def test_response_blocks_extracted_above_dashed_divider():
    """Approval 카드를 송출하기 **전** 에 위쪽 ⏺ 블록 두 개가 뽑혀야 한다
    (pre-approval flush 가 빈 결과를 받아 역순 전송되던 근본 원인)."""
    blocks = parser.extract_response_blocks(EDIT_APPROVAL_PANE)
    assert len(blocks) == 2
    assert blocks[0].startswith("⏺ 이제 bot.py 수정")
    assert blocks[1].startswith("⏺ Update(")
    # 두 블록 모두 approval 모달 본문 (`Do you want to`, `❯ 1. Yes`) 을
    # 포함하지 않아야 한다.
    joined = "\n".join(blocks)
    assert "Do you want to" not in joined
    assert "❯ 1. Yes" not in joined


def test_approval_box_excludes_dashed_divider_and_content_above():
    """`_approval_box` 가 `╌` divider 위쪽 (응답 영역) 을 포함하지 않아야 한다."""
    box = parser._approval_box(EDIT_APPROVAL_PANE)
    assert "Do you want to make this edit" in box
    assert "이제 bot.py 수정" not in box
    assert "Update(" not in box


def test_is_approval_still_true_with_dashed_divider():
    assert parser.is_approval(EDIT_APPROVAL_PANE) is True


def test_solid_divider_still_works():
    """기존 `─` (solid) divider 도 여전히 경계로 인식돼야 한다 (비-regression)."""
    pane = """\
⏺ 이전 응답 블록.

───────────────────────────────────────────────────────────────────────────────
 Do you want to proceed?
 ❯ 1. Yes
   2. No
"""
    lines, end = parser._response_region(pane)
    divider_idx = next(
        i for i, ln in enumerate(lines)
        if set(ln.strip()) <= {"─"} and len(ln.strip()) > 20
    )
    assert end == divider_idx
