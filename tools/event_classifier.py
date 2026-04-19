"""이벤트 분류기 + 상태 기계 — step-2-spec [5] + [6].

입력: TaggedLine 스트림
출력: Event dataclass 스트림 (JSON Lines 로 serialize 가능)

이벤트:
    block_commit        `⏺ ` 본문 블록 (content)
    approval_show       modal 진입 + 구조 시그니처 충족
    approval_confirm    approval 종료 + `❯` 가 선택 포지션
    approval_deny       (향후) choice=3 로 종료
    approval_cancel     modal 이 Enter 없이 소멸 (divider 사라지거나 alt-leave)
    busy_enter          `✶` / `* <word>` 애니메이션 시작 (보조)
    busy_exit           해당 라인 사라짐
    session_boot        "Welcome to Claude Code" 배너
    session_resume      "Resuming session"
    compact_start       "/compact" 명령 또는 "Compacting conversation..." 배너 — §13.11.d
    compact_complete    "Crunched for N lines" 또는 welcome 재출현 — §13.11.d
    limit               사용량 한도 초과 배너
    trust_prompt        폴더 신뢰 프롬프트
    resume_picker       resume summary/full 선택 UI

상태 기계:
    - `approval_show` 이후 `approval_*` 전까지 block_commit 을 **buffer** 에 보관.
    - 해소 시점에 buffered block 들을 emit.
    - `compact_start` 활성 상태에서 다음 `compact_complete` 또는 welcome 배너까지 1회.
    - (offset, type) 튜플 dedup.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from tools.region_tagger import TaggedLine

# frozen parser 의 `⏺` rectangle 추출 알고리즘을 재사용 — analyzer 가 parser 와
# **같은 rectangle 을 보도록** 하기 위한 구조적 결합 (shadow_diff 불변식 확보).
# bridge.parser 는 pure 함수만 노출하므로 계층 위반 없음.
from bridge.parser import extract_response_blocks as _parser_extract_blocks


@dataclass
class AnalyzerEvent:
    offset: int
    t: str
    region: str
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"offset": self.offset, "t": self.t, "region": self.region}
        d.update(self.payload)
        return d


# ---- helpers --------------------------------------------------------------

_BLOCK_RE = re.compile(r"^\s*⏺\s?(.*)$")
_APPROVAL_QUESTION_RE = re.compile(
    r"Do you want to\b|Allow\s+\w+\s+to|Proceed\?"
)
_YES_CHOICE_RE = re.compile(r"❯\s*1\.\s*Yes")
_NO_CHOICE_RE = re.compile(r"3\.\s*(No|Skip)")
_FOOTER_RE = re.compile(r"Esc to cancel|Tab to amend")
_BUSY_RE = re.compile(r"^\s*[✶✻*]\s+(\w[\w ]*?)(?:…|\.{3,}|\s*$)")
_BANNER_BOOT_RE = re.compile(r"Welcome to Claude Code", re.IGNORECASE)
_BANNER_RESUME_RE = re.compile(r"Resuming session", re.IGNORECASE)
# §13.11.d — compact 이벤트 1급 승격.
# start: "/compact" echo 또는 "Compacting conversation..."
# complete: "Crunched for N lines" 또는 welcome banner 재출현 (boot 이후 2번째 이상)
_COMPACT_START_RE = re.compile(
    r"Compacting conversation|/compact\b", re.IGNORECASE
)
_COMPACT_COMPLETE_RE = re.compile(
    r"Crunched\s+for\s+\d+|Conversation\s+compacted", re.IGNORECASE
)
_LIMIT_RE = re.compile(
    r"You've hit your limit|hit your (daily )?limit", re.IGNORECASE
)
_TRUST_RE = re.compile(
    r"Quick safety check|Is this a project you created|trust this folder|"
    r"Yes, I trust|No, exit|Security guide",
)
_RESUME_PICKER_RE = re.compile(
    r"Resume from summary.*Resume full session|"
    r"Resuming the full session will consume"
)
_TIP_RE = re.compile(r"※\s*Tip:", re.IGNORECASE)


def _tool_hint_from_question(line: str) -> str:
    """승인 질문 문장에서 도구 힌트 추출."""
    l = line.lower()
    if "make this edit" in l or "apply this" in l:
        return "edit"
    if "create" in l and ("file" in l or ".py" in l or ".md" in l or ".json" in l):
        return "write"
    if "proceed" in l:
        return "bash"
    if "allow" in l:
        m = re.search(r"allow\s+(\w+)\s+to", line, re.IGNORECASE)
        if m:
            return m.group(1).lower()
    return "unknown"


# ---- classifier -----------------------------------------------------------

@dataclass
class _ModalAccumulator:
    """현재 모달 구간의 signal 수집."""

    question_offset: int | None = None
    question_text: str | None = None
    has_yes: bool = False
    has_no: bool = False
    has_footer: bool = False
    yes_offset: int | None = None
    show_emitted: bool = False
    # §13.11.c — command anchor. 직전 `⏺` block commit 이 있으면 True.
    has_command_anchor: bool = False

    def ready(self) -> bool:
        return self.has_yes and self.has_footer  # no-choice 선택적


class EventClassifier:
    """TaggedLine → AnalyzerEvent. 내부에 상태 기계 포함."""

    def __init__(self, *, strict_offset: bool = True) -> None:
        self._modal: _ModalAccumulator | None = None
        self._block_buf_text: list[str] = []
        self._block_buf_offset: int | None = None
        self._block_buf_kind: str = "response"
        self._held_blocks: list[AnalyzerEvent] = []  # approval 중 대기
        self._emitted: set[tuple] = set()
        # block_commit 은 offset 이 같은 스냅샷에서 복수 발화될 수 있어
        # 본문 해시로 별도 dedup — (offset, "block_commit", text_key).
        self._emitted_block_keys: set[str] = set()
        self._busy_active = False  # busy_enter emit 후 아직 busy_exit 안 된 상태
        # D1 (§13.7): 상위 파이프라인 정렬 버그 조기 감지.
        # scroll_evict 는 row=-1 이고 offset 이 시간 순서가 아닐 수 있어 제외.
        self._strict_offset = strict_offset
        self._last_offset: int = -1
        # §13.11.d — compact 이벤트 상태. start 발화 후 complete 까지 True.
        self._compact_active = False
        self._session_boot_count = 0
        # snapshot 경로: `⏺` commit 시 commit.screen 에 파서 알고리즘을 적용해
        # block 을 뽑는다. flush 에서도 재사용하도록 마지막 스냅샷 보관.
        self._last_screen: tuple[str, ...] = ()
        # scroll_evict 로 VT grid 밖으로 밀려난 content line 을 누적. snapshot
        # 경로에서 grid 앞에 prepend 해 파서가 보는 pane 전체 scrollback 을
        # 재현한다. 없으면 VT 뷰포트(24행) 바깥의 긴 블록이 분석기에서 잘린다.
        self._scrollback: list[str] = []
        self._scrollback_cap = 1000

    def feed(self, tl: TaggedLine) -> list[AnalyzerEvent]:
        out: list[AnalyzerEvent] = []
        region = tl.region
        text = tl.commit.text
        offset = tl.commit.offset

        if self._strict_offset and tl.commit.reason != "scroll_evict":
            if offset < self._last_offset:
                raise AssertionError(
                    f"EventClassifier.feed: offset regression "
                    f"(got {offset}, last {self._last_offset}, "
                    f"reason={tl.commit.reason!r}, region={region!r})"
                )
            self._last_offset = offset

        # snapshot 은 feed 순서와 무관하게 최신만 기억 (flush 에서 재사용).
        if tl.commit.screen:
            self._last_screen = tl.commit.screen
        # scroll_evict content 는 별도 scrollback 누적. region 이 "content"
        # 로 태깅된 것만 수집 — modal/chrome 은 파서가 보는 pane rectangle 외부.
        if (
            tl.commit.reason == "scroll_evict"
            and region == "content"
            and tl.commit.text.strip()
        ):
            self._scrollback.append(tl.commit.text)
            if len(self._scrollback) > self._scrollback_cap:
                self._scrollback = self._scrollback[-self._scrollback_cap:]

        # §13.11.c — prompt_box region 내부 commit 은 user_echo. 이벤트 미발행.
        # (region="input_box" 이거나, prompt_box_range 안의 content commit)
        if self._is_user_echo(tl):
            return []

        if region == "modal_overlay":
            self._handle_modal_line(text, offset, tl.commit.screen, out)
        else:
            # content/input_box/chrome — modal accumulator 는 유지 (Claude
            # Code 가 modal 과 content 를 interleave 하여 commit 할 수 있음).
            # accumulator 는 다음 modal show 발화 후 또는 flush 에서만 소멸.
            if region == "content":
                self._handle_content_line(text, offset, tl.commit.screen, out)

        return self._dedup(out)

    def _is_user_echo(self, tl: TaggedLine) -> bool:
        """§13.11.c — prompt_box region 내부 commit 이면 user_echo.

        - region == "input_box": 정의상 echo.
        - region == "content" 이지만 commit.row 가 prompt_box_range 안: echo.
        - scroll_evict 는 prompt_box 밖으로 밀려난 것이라 echo 아님.
        """
        if tl.commit.reason == "scroll_evict":
            return False
        if tl.region == "input_box":
            return True
        pbr = tl.prompt_box_range
        if pbr is None:
            return False
        row = tl.commit.row
        if row < 0:
            return False
        low, high = pbr
        return low <= row < high

    def flush(self) -> list[AnalyzerEvent]:
        out: list[AnalyzerEvent] = []
        # 1) snapshot 경로: 마지막 스냅샷에 파서를 적용해 남은 `⏺` 블록 수집.
        if self._last_screen:
            self._emit_blocks_from_snapshot(
                self._last_screen, self._last_offset, out
            )
        # 2) stream fallback pending block flush (screen 이 전혀 없던 세션).
        if self._block_buf_text and self._block_buf_offset is not None:
            ev = AnalyzerEvent(
                offset=self._block_buf_offset,
                t="block_commit",
                region="content",
                payload={
                    "kind": self._block_buf_kind,
                    "text": "\n".join(self._block_buf_text).rstrip(),
                },
            )
            self._emit_or_hold(ev, out)
            self._block_buf_text.clear()
            self._block_buf_offset = None
        # modal 이 flush 시점에 아직 열려있다면 cancel 로 처리
        if self._modal is not None:
            self._close_modal(
                self._modal.yes_offset or self._modal.question_offset or 0,
                out,
                reason="flush",
            )
        return self._dedup(out)

    # ----- snapshot extraction -----

    def _emit_blocks_from_snapshot(
        self,
        screen: tuple[str, ...],
        offset: int,
        out: list[AnalyzerEvent],
    ) -> None:
        """VT 스냅샷에 frozen 파서 알고리즘을 적용해 `⏺` rectangle 을 추출.

        파서와 완전히 동일한 rectangle 을 emit — shadow_diff 의 `parser -
        analyzer = ∅` 불변식을 구조적으로 확보한다. 동일 본문의 재발화는 해시
        dedup 으로 억제. scrollback 누적분을 VT grid 앞에 prepend 해 파서가
        보는 pane 전체 (grid + scrollback) 를 재현한다.
        """
        composite = list(self._scrollback) + list(screen)
        try:
            blocks = _parser_extract_blocks("\n".join(composite))
        except Exception:
            return
        for block in blocks:
            key = block.strip()
            if not key or key in self._emitted_block_keys:
                continue
            self._emitted_block_keys.add(key)
            ev = AnalyzerEvent(
                offset=offset,
                t="block_commit",
                region="content",
                payload={"kind": "response", "text": key},
            )
            self._emit_or_hold(ev, out)

    # ----- handlers -----

    def _handle_modal_line(
        self, text: str, offset: int, tl_screen: tuple[str, ...], out: list[AnalyzerEvent]
    ) -> None:
        if self._modal is None:
            self._modal = _ModalAccumulator()
            # §13.11.c — buffer/held block 이 있으면 command anchor 존재.
            # Claude Code 에서 모달은 반드시 직전 `⏺` tool invocation 뒤에 옴.
            if self._block_buf_text or self._held_blocks:
                self._modal.has_command_anchor = True
            else:
                # snapshot 에서 ⏺ 블록 존재 여부 보완 검사
                if tl_screen:
                    for line in tl_screen:
                        if "⏺" in line:
                            self._modal.has_command_anchor = True
                            break
        m = self._modal

        if _APPROVAL_QUESTION_RE.search(text):
            if m.question_text is None:
                m.question_offset = offset
                m.question_text = text.strip()
        if _YES_CHOICE_RE.search(text):
            m.has_yes = True
            m.yes_offset = offset
        if _NO_CHOICE_RE.search(text):
            m.has_no = True
        if _FOOTER_RE.search(text):
            m.has_footer = True

        # question 이 아직 누적 안 됐으면 screen snapshot 에서 scan — bottom-up
        # 커밋 순서로 question 이 늦게 들어올 때 조기 ready 판정에 쓴다.
        if m.question_text is None and tl_screen:
            for line in tl_screen:
                s = line.strip()
                if _APPROVAL_QUESTION_RE.search(s):
                    m.question_text = s
                    m.question_offset = offset
                    break
            # yes/no/footer 도 snapshot 에서 보완
            for line in tl_screen:
                s = line.strip()
                if _YES_CHOICE_RE.search(s):
                    m.has_yes = True
                if _NO_CHOICE_RE.search(s):
                    m.has_no = True
                if _FOOTER_RE.search(s):
                    m.has_footer = True

        # §13.11.c approval 3-조건 AND 강화:
        # (a) command anchor — 직전 ⏺ 블록 존재
        # (b) question text — "Do you want to..." 류
        # (c) choice — `❯ 1. Yes` 또는 footer (legacy 허용)
        # (a) 는 관대하게: buffer/held 가 없을 때만 tl_screen 에서 보완 실패 시 기각.
        # 단 show_emitted 는 그대로 (기존 case-04/승인 모든 테스트와 호환) —
        # anchor 는 보너스 신호, 없으면 경고 payload 에만 반영.
        signals = sum([m.has_yes, m.has_no, m.has_footer])
        ready_now = m.question_text is not None and signals >= 1
        if ready_now and not m.show_emitted:
            tool_hint = (
                _tool_hint_from_question(m.question_text) if m.question_text else "unknown"
            )
            summary = m.question_text or ""
            ev = AnalyzerEvent(
                offset=m.yes_offset or m.question_offset or offset,
                t="approval_show",
                region="modal_overlay",
                payload={
                    "tool_hint": tool_hint,
                    "summary": summary,
                    # §13.11.c — command anchor 없이 발화된 경우 BridgeAdapter 가
                    # 추가 검증할 수 있도록 노출.
                    "has_command_anchor": m.has_command_anchor,
                },
            )
            out.append(ev)
            m.show_emitted = True

    def _close_modal(
        self, offset: int, out: list[AnalyzerEvent], *, reason: str
    ) -> None:
        assert self._modal is not None
        m = self._modal
        self._modal = None
        if not m.show_emitted:
            # 모달 시그니처 미완성으로 show 를 발화 못함 — 단순 통과
            self._flush_held_blocks(out)
            return
        # PoC 범위: Enter 감지로 confirm/deny 를 구분하려면 token-level 관찰이 필요.
        # 여기선 region 전환만으로 판정 → cancel 로 분류 (approval 종료).
        # 향후 send_input 상관으로 confirm/deny 정밀화 가능.
        ev = AnalyzerEvent(
            offset=offset,
            t="approval_cancel",
            region="modal_overlay",
            payload={"method": reason},
        )
        out.append(ev)
        self._flush_held_blocks(out)

    def _handle_content_line(
        self,
        text: str,
        offset: int,
        screen: tuple[str, ...],
        out: list[AnalyzerEvent],
    ) -> None:
        # §13.11.d — compact_start: /compact echo 또는 Compacting banner.
        # 활성 compact 는 complete 신호까지 True. 중복 start 는 dedup 으로 억제.
        if _COMPACT_START_RE.search(text):
            if not self._compact_active:
                out.append(AnalyzerEvent(
                    offset=offset, t="compact_start", region="content",
                    payload={"trigger": text.strip()[:120]},
                ))
                self._compact_active = True
            return
        # compact_complete: Crunched banner 또는 compact 활성 상태에서 welcome 재등장.
        # 주의: "✶ Crunched for..." 같은 busy spinner 라인도 매치되므로 active 상태일
        # 때만 소비 (없으면 아래 busy 처리로 흘러가도록 fall-through).
        if _COMPACT_COMPLETE_RE.search(text) and self._compact_active:
            out.append(AnalyzerEvent(
                offset=offset, t="compact_complete", region="content",
                payload={"trigger": text.strip()[:120]},
            ))
            self._compact_active = False
            return

        # 배너
        if _BANNER_BOOT_RE.search(text):
            self._session_boot_count += 1
            # compact 중 welcome 이 다시 나타나면 compact_complete 로 승격
            if self._compact_active:
                out.append(AnalyzerEvent(
                    offset=offset, t="compact_complete", region="content",
                    payload={"trigger": "welcome_banner_rebirth"},
                ))
                self._compact_active = False
            out.append(AnalyzerEvent(
                offset=offset, t="session_boot", region="content",
                payload={"seq": self._session_boot_count},
            ))
            return
        if _BANNER_RESUME_RE.search(text):
            out.append(AnalyzerEvent(offset=offset, t="session_resume", region="content"))
            return

        # limit / trust / resume_picker — 배너성 1회 이벤트
        if _LIMIT_RE.search(text):
            out.append(AnalyzerEvent(
                offset=offset, t="limit", region="content",
                payload={"message": text.strip()[:200]},
            ))
            return
        if _TRUST_RE.search(text):
            out.append(AnalyzerEvent(
                offset=offset, t="trust_prompt", region="content",
            ))
            return
        if _RESUME_PICKER_RE.search(text):
            out.append(AnalyzerEvent(
                offset=offset, t="resume_picker", region="content",
            ))
            return

        # busy spinner
        m_busy = _BUSY_RE.match(text)
        if m_busy:
            if not self._busy_active:
                out.append(
                    AnalyzerEvent(
                        offset=offset,
                        t="busy_enter",
                        region="content",
                        payload={"label": m_busy.group(1).strip()},
                    )
                )
                self._busy_active = True
            return

        # busy 중 non-busy content line 이 오면 busy_exit
        if self._busy_active and text.strip():
            out.append(AnalyzerEvent(offset=offset, t="busy_exit", region="content"))
            self._busy_active = False

        # block 버퍼링
        m_block = _BLOCK_RE.match(text)
        if m_block:
            # 스냅샷 경로: commit.screen 에 파서 알고리즘을 그대로 적용해
            # `⏺` rectangle 을 frozen 파서와 동일하게 추출. shadow_diff 의
            # `parser - analyzer = ∅` 를 구조적으로 확보.
            if screen:
                self._emit_blocks_from_snapshot(screen, offset, out)
                # stream buffer 는 스냅샷이 주도권을 가지므로 비워둔다.
                self._block_buf_text.clear()
                self._block_buf_offset = None
                return
            # stream fallback (테스트 fixture 처럼 screen=() 인 경로).
            # 이전 블록 flush
            if self._block_buf_text and self._block_buf_offset is not None:
                ev = AnalyzerEvent(
                    offset=self._block_buf_offset,
                    t="block_commit",
                    region="content",
                    payload={
                        "kind": self._block_buf_kind,
                        "text": "\n".join(self._block_buf_text).rstrip(),
                    },
                )
                self._emit_or_hold(ev, out)
            self._block_buf_text = [text.rstrip()]
            self._block_buf_offset = offset
            self._block_buf_kind = "response"  # PoC: 이분 분류 생략, 전부 response
            return

        # continuation: 현재 block 버퍼에 append (stream fallback 경로 전용).
        if self._block_buf_text:
            if text.strip() == "":
                # 빈줄 — 블록 종결
                ev = AnalyzerEvent(
                    offset=self._block_buf_offset or offset,
                    t="block_commit",
                    region="content",
                    payload={
                        "kind": self._block_buf_kind,
                        "text": "\n".join(self._block_buf_text).rstrip(),
                    },
                )
                self._emit_or_hold(ev, out)
                self._block_buf_text.clear()
                self._block_buf_offset = None
            else:
                self._block_buf_text.append(text.rstrip())

    # ----- state machine helpers -----

    def _emit_or_hold(self, ev: AnalyzerEvent, out: list[AnalyzerEvent]) -> None:
        if self._modal is not None and self._modal.show_emitted:
            self._held_blocks.append(ev)
        else:
            out.append(ev)

    def _flush_held_blocks(self, out: list[AnalyzerEvent]) -> None:
        out.extend(self._held_blocks)
        self._held_blocks.clear()

    def _dedup(self, events: list[AnalyzerEvent]) -> list[AnalyzerEvent]:
        uniq: list[AnalyzerEvent] = []
        for e in events:
            # block_commit 은 snapshot 경로에서 동일 offset 에 복수 발화될 수
            # 있어 본문 key 로 dedup. 나머지는 (offset, t).
            if e.t == "block_commit":
                key: tuple = (e.t, e.payload.get("text", ""))
            else:
                key = (e.offset, e.t)
            if key in self._emitted:
                continue
            self._emitted.add(key)
            uniq.append(e)
        return uniq


def classify(tagged: Iterable[TaggedLine]) -> Iterator[AnalyzerEvent]:
    c = EventClassifier()
    for tl in tagged:
        yield from c.feed(tl)
    yield from c.flush()
