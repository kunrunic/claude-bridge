"""pane 출력 해석 — pure 함수만.

모든 함수는 입력 문자열을 받아 결과를 리턴한다. I/O/전역 상태 없음.
이 파일에 tmux/telegram 모듈 의존이 들어오면 계층 위반이다.
"""
from __future__ import annotations

import re

from .config import APPROVAL_SCAN_LINES, BUSY_CHECK_TAIL, COMPACT_SCAN_LINES

# -- 패턴 --------------------------------------------------------------------

# Claude Code 승인 프롬프트 감지 패턴.
# "Do you want to proceed" 가 정식 승인창 표식.
# ❯ 1. Yes 단독은 신뢰 프롬프트와 충돌하므로 사용 X.
APPROVAL_RE = re.compile(
    r"Do you want to proceed|"
    r"Allow\s+\w+\s+to|Proceed\?|\(Y/n\)|\(y/N\)"
)
# 폴더 신뢰 프롬프트 (새 디렉토리 진입 시 한 번 뜸)
TRUST_RE = re.compile(
    r"Quick safety check|"
    r"Is this a project you created|"
    r"trust this folder|"
    r"Yes, I trust|"
    r"No, exit|"
    r"Security guide"
)
# Claude 가 "작업 중" 상태 — "esc to interrupt" 만 신뢰 가능한 활성 신호.
BUSY_RE = re.compile(r"esc to interrupt")
# 사용량 한도 초과 감지
LIMIT_RE = re.compile(r"You've hit your limit|hit your (daily )?limit", re.IGNORECASE)
# 컨텍스트 압축 이벤트 (자동 /compact 또는 'Crunched for N' 요약 라인)
COMPACT_RE = re.compile(r"Compacting conversation|Crunched\s+for\s+\d+", re.IGNORECASE)
# Context limit 도달
CONTEXT_LIMIT_RE = re.compile(r"Context limit reached", re.IGNORECASE)
# /compact 가 API 에러로 실패한 경우
COMPACT_ERROR_RE = re.compile(r"Error during compaction", re.IGNORECASE)
ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


# -- 단순 판정 ----------------------------------------------------------------

def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def is_trust_prompt(text: str) -> bool:
    return bool(TRUST_RE.search(text))


def is_approval(text: str) -> bool:
    return bool(APPROVAL_RE.search(text))


def is_busy(text: str) -> bool:
    """Claude 가 처리 중인지 — 현재 상태바 영역(마지막 divider 이하)만 검사.
    scrollback 에 남은 과거 상태바의 'esc to interrupt' 로 인한 오탐을 막는다.
    """
    lines = text.splitlines()
    # 마지막 ~10줄 안에서 divider 찾기 (상태바 구조: divider/❯/divider/status)
    search_window = max(0, len(lines) - 10)
    for i in range(len(lines) - 1, search_window - 1, -1):
        s = lines[i].strip()
        if s and len(s) > 20 and all(c in "─" for c in s):
            tail = "\n".join(lines[i:])
            return bool(BUSY_RE.search(tail))
    # divider 못 찾으면 마지막 BUSY_CHECK_TAIL 줄로 fallback
    tail = "\n".join(lines[-BUSY_CHECK_TAIL:])
    return bool(BUSY_RE.search(tail))


def has_compaction(text: str) -> bool:
    """pane tail 에 컨텍스트 압축 이벤트 흔적이 있는지."""
    tail = "\n".join(text.splitlines()[-COMPACT_SCAN_LINES:])
    return bool(COMPACT_RE.search(tail))


def has_context_limit(text: str) -> bool:
    """'Context limit reached' 표식이 tail 에 있는지."""
    tail = "\n".join(text.splitlines()[-COMPACT_SCAN_LINES:])
    return bool(CONTEXT_LIMIT_RE.search(tail))


def has_compaction_error(text: str) -> bool:
    """'Error during compaction' — /compact 가 API 단에서 거절된 경우."""
    tail = "\n".join(text.splitlines()[-COMPACT_SCAN_LINES:])
    return bool(COMPACT_ERROR_RE.search(tail))


# -- 상태 라벨 ----------------------------------------------------------------

_STATUS_LINE_RE = re.compile(
    r"(?:[·✻⋯*]\s*)?"
    r"(Compacting[^\n│]*|Thinking[^\n│]*|Cerebrating[^\n│]*|Pondering[^\n│]*|"
    r"Cogitat\w+[^\n│]*|Crunching[^\n│]*|Running[^\n│]*|Tool use[^\n│]*)",
    re.IGNORECASE,
)

_CC_TIMER_RE = re.compile(r"\(\s*(\d+)\s*s\b[^)]*\)?")


def _format_busy_status(label: str, cc_sec: str | None, bot_elapsed: int) -> str:
    """상태 메시지 포맷.

    Claude 타이머가 있으면:   ⏳ Compacting conversation · 🧠 26s · 🤖 28s
    없으면:                    ⏳ Thinking · 🤖 12s
    """
    base = f"⏳ {label}"
    if cc_sec:
        return f"{base} · 🧠 {cc_sec} · 🤖 {bot_elapsed}s"
    return f"{base} · 🤖 {bot_elapsed}s"


def busy_status(text: str) -> tuple[str, str | None]:
    """화면에서 진행 상태 라인 추출 → (액션, Claude 내장 초).

    예: "✻ Compacting conversation… (26s · esc to interrupt)"
        → ("Compacting conversation…", "26s")
    Claude 타이머가 없으면 두 번째 값은 None.
    """
    tail_lines = text.splitlines()[-25:]
    for line in reversed(tail_lines):
        m = _STATUS_LINE_RE.search(line)
        if m:
            label = m.group(1).strip()
            for cut in ("esc to interrupt", "ctrl+"):
                idx = label.lower().find(cut)
                if idx > 0:
                    label = label[:idx].strip()
            # Claude 타이머 `(Ns ...)` 분리
            cc_sec: str | None = None
            tm = _CC_TIMER_RE.search(label)
            if tm:
                cc_sec = f"{tm.group(1)}s"
                label = _CC_TIMER_RE.sub("", label).strip(" ·-—|")
            # 꼬리 장식 문자 정리
            label = label.rstrip(" ·…").strip()
            if not label:
                label = "작업 중"
            return label[:80], cc_sec
    return "작업 중", None


# -- ⏺ 블록 추출 --------------------------------------------------------------

def _response_region(text: str) -> tuple[list[str], int]:
    """응답 영역 경계 감지 → (lines, end_idx).

    end_idx: 응답 영역 끝 (exclusive). 승인 박스/입력창 divider/Welcome 전.
    """
    lines = text.splitlines()

    def is_divider(line: str) -> bool:
        s = line.strip()
        return bool(s) and len(s) > 20 and all(c in "─" for c in s)

    end = len(lines)

    # 1) 승인 박스/입력창 경계 감지
    prompt_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if "Do you want to proceed" in line or line == "Do you want to":
            prompt_idx = i
            break
    if prompt_idx > 0:
        for i in range(prompt_idx - 1, max(-1, prompt_idx - 60), -1):
            if is_divider(lines[i]):
                end = min(end, i)
                break

    # 2) 입력창 divider (마지막 ~15줄)
    divider_positions = []
    for i in range(len(lines) - 1, max(-1, len(lines) - 15), -1):
        if is_divider(lines[i]):
            divider_positions.append(i)
        if "Welcome back" in lines[i] or "Claude Code v" in lines[i]:
            end = min(end, i)
    if divider_positions:
        end = min(end, min(divider_positions))

    return lines, end


def extract_last_response(text: str) -> str:
    """화면 출력에서 Claude 의 마지막 응답(마지막 ⏺ 블록)만 추출."""
    lines, end = _response_region(text)

    # 마지막 ⏺ (Claude 응답 시작) 찾기
    start = -1
    for i in range(end - 1, -1, -1):
        if lines[i].lstrip().startswith("⏺"):
            start = i
            break

    if start < 0:
        return ""

    # ⏺ 이후에 새 ❯ 사용자 입력이 있으면 그 전까지만
    for i in range(start + 1, end):
        if lines[i].lstrip().startswith("❯ "):
            end = i
            break

    return "\n".join(lines[start:end]).strip()


def extract_response_blocks(text: str) -> list[str]:
    """응답 영역 내 모든 ⏺ 블록을 순서대로 추출.

    마지막 블록은 아직 스트리밍 중일 수 있음 (성장 중).
    호출자가 블록 단위 dedup 을 하거나 "마지막 제외" 정책을 적용하기 좋게 리스트로 반환.
    """
    lines, end = _response_region(text)

    # 마지막 ❯ 사용자 입력 이후부터 end 사이에서 ⏺ 시작 위치 수집.
    # 내용이 비어있는 ❯ (입력 박스) 는 user prompt 로 간주하지 않는다 —
    # 실제 panel 은 대개 divider 로 input box 를 구분하지만, divider 가 없는
    # 경우에도 "❯ 만 있고 뒤에 텍스트가 없는" 줄은 제출된 입력이 아니라 대기 중인
    # 입력 박스이므로 스킵하고 그 위의 '제출된 ❯' 를 찾는다.
    user_prompt_idx = -1
    for i in range(end - 1, -1, -1):
        s = lines[i].lstrip()
        if not s.startswith("❯"):
            continue
        rest = s[1:].strip()
        if not rest:
            continue   # 빈 입력 박스 — 스킵
        user_prompt_idx = i
        break
    scan_start = user_prompt_idx + 1

    starts = [
        i for i in range(scan_start, end)
        if lines[i].lstrip().startswith("⏺")
    ]
    if not starts:
        return []

    blocks: list[str] = []
    for idx, s in enumerate(starts):
        e = starts[idx + 1] if idx + 1 < len(starts) else end
        block = "\n".join(lines[s:e]).rstrip()
        if block:
            blocks.append(block)
    return blocks


_ACTIVE_BLOCK_RE = re.compile(
    r"Running…|Waiting…|ctrl\+b.*background", re.IGNORECASE
)


def _is_block_active(block: str) -> bool:
    """블록이 아직 실행 중인 도구를 포함하는지 판정 (타이머가 갱신되는 블록)."""
    tail = block.rsplit("\n", 3)[-3:]
    return bool(_ACTIVE_BLOCK_RE.search("\n".join(tail)))


# -- 승인 박스/모델 피커 ------------------------------------------------------

def summarize_approval(text: str) -> str:
    """승인 요청 박스에서 도구명만 추출
    (상세 내용은 이미 ⏺ Bash(...) 로 전달됨)."""
    raw_lines = text.splitlines()

    proceed_idx = -1
    for i in range(len(raw_lines) - 1, -1, -1):
        if "Do you want to" in raw_lines[i]:
            proceed_idx = i
            break
    if proceed_idx < 0:
        return "승인"

    start = max(0, proceed_idx - APPROVAL_SCAN_LINES)
    for ln in raw_lines[start:proceed_idx]:
        s = ln.strip()
        m = re.match(r"^(Bash|Edit|Write|Read|MultiEdit|WebFetch|Grep|Glob|Task)\b", s)
        if m:
            return m.group(1)
    return "승인"


def _approval_box(text: str) -> str:
    """'Do you want to proceed?' 위쪽 가장 가까운 divider~prompt 사이만 추출."""
    lines = text.splitlines()
    proceed = -1
    for i in range(len(lines) - 1, -1, -1):
        if "Do you want to" in lines[i]:
            proceed = i
            break
    if proceed < 0:
        return text[-400:]
    start = max(0, proceed - APPROVAL_SCAN_LINES)
    for i in range(proceed - 1, start - 1, -1):
        s = lines[i].strip()
        if s and len(s) > 20 and all(c in "─" for c in s):
            start = i + 1
            break
    end = min(len(lines), proceed + 6)
    return "\n".join(lines[start:end]).strip()


_PICKER_OPT_RE = re.compile(r"^\s*(?P<arrow>❯)?\s*(?P<num>\d+)\.\s+(?P<name>.+?)(?:\s{2,}|$)")


def _parse_model_options(lines: list[str]) -> list[dict]:
    """/model 피커 라인에서 옵션 목록 추출.

    리턴: [{"num": int, "name": str, "current": bool}, ...]
    - 같은 번호가 여러 번 보이면 마지막 것만 유지 (TUI 재렌더 안전).
    """
    found: dict[int, dict] = {}
    for ln in lines:
        m = _PICKER_OPT_RE.match(ln)
        if not m:
            continue
        num = int(m.group("num"))
        name = m.group("name").strip()
        if len(name) > 40:
            name = name[:40].rstrip()
        found[num] = {
            "num": num,
            "name": name,
            "current": bool(m.group("arrow")),
        }
    return [found[k] for k in sorted(found)]


def _find_picker_cursor(lines: list[str]) -> int | None:
    """현재 ❯ 커서가 가리키는 옵션 번호."""
    for ln in lines:
        m = _PICKER_OPT_RE.match(ln)
        if m and m.group("arrow"):
            return int(m.group("num"))
    return None
