"""Region 태거 — step-2-spec [4].

LineCommit 스트림에 region 속성을 부여한다.

규칙:
- in_alt=True → **modal_overlay**.
- row=-1 (scroll_evict) → **content** (scrollback 으로 밀려난 본문).
- 상하 `─` 연속 divider (길이 ≥ 32) 사이 구간 → **modal_overlay**.
- 마지막 non-chrome 구간의 `❯ ` 시작 row + 그 이후 divider 직전까지 → **input_box**.
- `⏵⏵ bypass permissions` 같은 bottom HUD → **chrome**.
- 그 외 → **content**.

현실적 단순화 (PoC):
- commit 스트림은 **순서대로** 들어오므로 "현재 상태" 기반으로 tagging 가능.
- 최근 divider 시점을 기억했다가 다음 divider 까지 modal/input 여부 결정.
- Claude Code 는 모달을 alt-screen 에 거의 안 그림 (primary 내 box-drawing) → 두
  경로 모두 커버.

출력: TaggedLine (LineCommit + region).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

from tools.line_commit import LineCommit

Region = str  # "content" | "input_box" | "modal_overlay" | "chrome"


@dataclass
class TaggedLine:
    commit: LineCommit
    region: Region
    # §13.11.c — prompt_box 영역 row range. (low, high_exclusive) 또는 None.
    # EventClassifier 가 echo 판정 및 approval 3-조건 AND 에 사용.
    prompt_box_range: tuple[int, int] | None = None


def _is_divider(text: str, min_len: int = 32) -> bool:
    """box-drawing 가로 divider 판정."""
    stripped = text.strip()
    if len(stripped) < min_len:
        return False
    # 문자 다양성이 낮아야 함 — ─/╌/=/- 중 하나가 대부분
    divider_chars = set("─╌═-")
    count = sum(1 for ch in stripped if ch in divider_chars)
    return count >= len(stripped) * 0.9


def _is_input_box_header(text: str) -> bool:
    """`❯ ` 또는 `> ` 로 시작하는 입력 프롬프트 라인."""
    stripped = text.lstrip()
    return stripped.startswith("❯ ") or stripped.startswith("> ") or stripped == "❯"


def _is_chrome(text: str) -> bool:
    """bottom HUD 힌트."""
    lowered = text.lower()
    markers = (
        "bypass permissions",
        "? for shortcuts",
        "esc to cancel",
        "ctrl+c to",
        "shift+tab",
        "tab to amend",
    )
    return any(m in lowered for m in markers)


def _looks_like_modal_body(text: str) -> bool:
    """modal 박스 내부 문구 휴리스틱."""
    stripped = text.strip()
    if stripped.startswith("Do you want to"):
        return True
    if stripped.startswith(("❯ 1.", "  1.", "  2.", "  3.", "  4.")):
        return True
    return False


class RegionTagger:
    """LineCommit 시퀀스에 region 부여.

    상태:
    - `in_modal`: 현재 commit 스트림이 모달 구간 안에 있는가.
    - 지난 divider 이후 "modal body" 문구를 봤는지 여부로 modal 경계를 휴리스틱
      판정 (Claude Code 는 divider 1개 위/아래로 모달을 그리는 패턴).
    """

    def __init__(self) -> None:
        self.in_modal = False
        # divider 를 본 직후 '다음 라인' 이 modal 로 넘어갈지 평가하는 래치.
        # 단순 경계 카운터 대신, "modal 힌트 라인을 만나면 in_modal True, 다음
        # divider 에서 False" 로 작동.
        self._saw_divider_recently = False

    def tag(self, commit: LineCommit) -> TaggedLine:
        # prompt_box_range 는 modal 판정과 무관하게 snapshot 에서 계산
        # (EventClassifier 가 user_echo 판정에 사용 — §13.11.c).
        pb_range = _detect_prompt_box_range(commit.screen) if commit.screen else None

        # alt-screen 은 무조건 modal_overlay
        if commit.in_alt:
            return TaggedLine(commit=commit, region="modal_overlay",
                              prompt_box_range=pb_range)

        # scroll evicted 는 content (모달이 scroll 밀어내는 일은 없다고 가정)
        if commit.reason == "scroll_evict":
            return TaggedLine(commit=commit, region="content",
                              prompt_box_range=pb_range)

        # **구조적 검사**: 현재 commit 의 row 가 VT 스냅샷에서 모달 envelope
        # 안에 있는지 — 위쪽 divider 근방 + Yes/No 선택지 / 질문 / footer 가
        # 보이면 modal_overlay 로 override. 이게 sequential 상태보다 견고 —
        # Claude Code 가 bottom-up 으로 그려서 commit 순서와 row 순서가
        # 어긋나도 결과 일관.
        if _row_in_modal_envelope(commit.screen, commit.row):
            return TaggedLine(commit=commit, region="modal_overlay",
                              prompt_box_range=pb_range)

        text = commit.text

        if _is_divider(text):
            # divider 자체의 region: modal 이면 modal_overlay, 아니면 content
            region: Region = "modal_overlay" if self.in_modal else "content"
            # divider 는 modal 경계 힌트 — 래치 토글 로직
            if self.in_modal:
                # modal 종료 divider
                self.in_modal = False
                self._saw_divider_recently = False
            else:
                self._saw_divider_recently = True
            return TaggedLine(commit=commit, region=region, prompt_box_range=pb_range)

        if _is_chrome(text):
            # chrome 이지만 "Esc to cancel" / "Tab to amend" 는 modal footer 이기도 함
            if self.in_modal or _looks_like_modal_body_context(text):
                return TaggedLine(commit=commit, region="modal_overlay",
                                  prompt_box_range=pb_range)
            return TaggedLine(commit=commit, region="chrome", prompt_box_range=pb_range)

        if _is_input_box_header(text):
            # 모달 안의 `❯ 1. Yes` 는 modal, 하단의 `❯ ` 는 input_box
            if self.in_modal:
                return TaggedLine(commit=commit, region="modal_overlay",
                                  prompt_box_range=pb_range)
            return TaggedLine(commit=commit, region="input_box",
                              prompt_box_range=pb_range)

        if _looks_like_modal_body(text) or self._saw_divider_recently:
            # modal 내부 문구를 만나면 modal 시작 확정
            if _looks_like_modal_body(text):
                self.in_modal = True
                self._saw_divider_recently = False
                return TaggedLine(commit=commit, region="modal_overlay",
                                  prompt_box_range=pb_range)
            # divider 직후 일반 텍스트 — content 로 분류, 래치 소멸
            self._saw_divider_recently = False
            if self.in_modal:
                return TaggedLine(commit=commit, region="modal_overlay",
                                  prompt_box_range=pb_range)
            return TaggedLine(commit=commit, region="content",
                              prompt_box_range=pb_range)

        if self.in_modal:
            return TaggedLine(commit=commit, region="modal_overlay",
                              prompt_box_range=pb_range)
        return TaggedLine(commit=commit, region="content", prompt_box_range=pb_range)

    def tag_many(self, commits: Iterable[LineCommit]) -> Iterator[TaggedLine]:
        for c in commits:
            yield self.tag(c)


def _looks_like_modal_body_context(text: str) -> bool:
    """chrome 으로 분류된 문구 중 실제는 modal footer 인 경우 구별용."""
    lowered = text.lower()
    return "esc to cancel" in lowered or "tab to amend" in lowered


def _detect_prompt_box_range(screen: tuple[str, ...]) -> tuple[int, int] | None:
    """VT 스냅샷에서 하단 입력 박스의 row 범위를 찾는다 (§13.11.c).

    Claude Code 의 입력 박스는 하단에 `╭──...─╮` / `│ > ... │` / `╰──...─╯` 형태의
    박스 drawing 으로 그려진다. 다만 PoC 환경에서는 단순 `─` divider 로 대체되기도
    하므로, **하단에서 위로 스캔**하며 다음 규칙으로 범위 결정:

    1. 아래쪽에서 올라가며 `❯ ` 또는 `> ` 로 시작하는 input header 를 찾음.
       (modal 의 `❯ 1. Yes` 는 제외 — question/divider signal 가 함께 있는지로 구분.)
    2. 해당 row 위/아래 각 ±3 줄 내에서 divider 가 있으면 그 사이를 prompt_box 로 판정.
    3. 없으면 header row 단독으로 (row, row+1) 범위.

    modal envelope 이 같은 위치에 있으면 (`_row_in_modal_envelope` 이 True) prompt_box
    판정은 포기 (None). modal 이 끝날 때까지 echo 억제는 region=modal_overlay 로 대체됨.
    """
    if not screen:
        return None
    n = len(screen)

    # 1. 하단에서 위로 `❯ ` / `> ` 헤더 찾기
    header_row: int | None = None
    for i in range(n - 1, -1, -1):
        line = screen[i]
        if _is_input_box_header(line):
            # modal footer 시그니처가 주변 ±3 에 있으면 skip
            lo = max(0, i - 3)
            hi = min(n, i + 4)
            modal_hint = False
            for j in range(lo, hi):
                s = screen[j]
                if _re.search(r"Do you want to|Esc to cancel|Tab to amend", s):
                    modal_hint = True
                    break
                if _re.search(r"❯\s*1\.\s*(Yes|Reset)", s):
                    modal_hint = True
                    break
            if modal_hint:
                continue
            header_row = i
            break

    if header_row is None:
        return None

    # 2. header 위/아래 divider 로 경계 결정
    top = header_row
    bottom = header_row + 1
    for j in range(header_row - 1, max(-1, header_row - 4), -1):
        if _is_divider(screen[j]):
            top = j
            break
    for j in range(header_row + 1, min(n, header_row + 4)):
        if _is_divider(screen[j]):
            bottom = j + 1
            break
        if screen[j].strip():
            # 비빈 non-divider 를 만나면 경계 확대 중지
            bottom = j + 1

    return (top, bottom)


import re as _re  # noqa: E402 — 하단 helper 에서만 사용


def _row_in_modal_envelope(screen: tuple[str, ...], row: int) -> bool:
    """row 가 현재 screen snapshot 의 modal envelope 안에 있는지.

    envelope 정의: 같은 snapshot 내에서 row 주변 (위/아래 각 8줄) 중
    - divider (─/╌ 연속) 최소 1줄 AND
    - 승인 시그니처 중 최소 2가지:
        (a) "Do you want to" / "Allow .* to" / "Proceed\\?"
        (b) `❯\\s*1\\.\\s*Yes` 또는 `❯\\s*1\\.\\s*Reset`
        (c) `2\\.\\s*Yes, ` 또는 `3\\.\\s*(No|Skip)`
        (d) `Esc to cancel` / `Tab to amend`
    이 보이면 True.
    """
    if not screen or row < 0 or row >= len(screen):
        return False

    lo = max(0, row - 8)
    hi = min(len(screen), row + 9)  # exclusive

    has_divider = False
    has_a = has_b = has_c = has_d = False
    import re as _re
    for i in range(lo, hi):
        s = screen[i].strip()
        if not s:
            continue
        if _is_divider(screen[i]):
            has_divider = True
        if _re.search(r"Do you want to|Allow\s+\w+\s+to|Proceed\?", s):
            has_a = True
        if _re.search(r"❯\s*1\.\s*(Yes|Reset)", s):
            has_b = True
        if _re.search(r"(?<!\d)[23]\.\s*(Yes,|No|Skip)", s):
            has_c = True
        if _re.search(r"Esc to cancel|Tab to amend", s):
            has_d = True
    signals = sum([has_a, has_b, has_c, has_d])
    return has_divider and signals >= 2
