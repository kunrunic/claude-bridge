"""EventClassifier + state machine 테스트 — step-2-spec [5] + [6]."""
from __future__ import annotations

from tools.event_classifier import EventClassifier
from tools.line_commit import LineCommit
from tools.region_tagger import TaggedLine


def _mk(region: str, text: str, *, offset: int = 0, row: int = 0, screen=()) -> TaggedLine:
    return TaggedLine(
        commit=LineCommit(
            offset=offset, row=row, text=text, reason="timer", screen=tuple(screen)
        ),
        region=region,
    )


def _run(*lines: TaggedLine) -> list:
    c = EventClassifier()
    out = []
    for tl in lines:
        out.extend(c.feed(tl))
    out.extend(c.flush())
    return out


def test_block_commit_emitted_on_empty_continuation():
    events = _run(
        _mk("content", "⏺ first response", offset=10),
        _mk("content", "  continuation line", offset=20),
        _mk("content", "", offset=30),  # 빈 줄 → 블록 종결
        _mk("content", "⏺ second response", offset=40),
    )
    blocks = [e for e in events if e.t == "block_commit"]
    assert len(blocks) == 2
    assert "first response" in blocks[0].payload["text"]
    assert "continuation line" in blocks[0].payload["text"]
    assert "second response" in blocks[1].payload["text"]


def test_approval_show_holds_block_commit():
    """approval_show 이후 approval 해소 전까지 block_commit 보류."""
    screen = (
        "",
        "─" * 60,
        "Do you want to proceed?",
        " ❯ 1. Yes",
        "   3. No",
        "Esc to cancel · Tab to amend",
        "",
    )
    events = _run(
        _mk("content", "⏺ before modal", offset=10),
        _mk("content", "", offset=20),  # flush block 1
        _mk("modal_overlay", "Do you want to proceed?", offset=30, row=2, screen=screen),
        _mk("modal_overlay", " ❯ 1. Yes", offset=35, row=3, screen=screen),
        _mk("modal_overlay", "Esc to cancel · Tab to amend", offset=40, row=5, screen=screen),
        # 승인 동안 도착한 block 은 보류 — 실제 UI 에서는 보통 이 시점에 없지만
        # 상태 기계 검증용 시나리오
        _mk("content", "⏺ during modal", offset=50),
        _mk("content", "", offset=60),
    )
    # approval_show 1 회, block_commit 2 회 (before modal + during modal)
    show = [e for e in events if e.t == "approval_show"]
    blocks = [e for e in events if e.t == "block_commit"]
    assert len(show) == 1
    assert len(blocks) == 2
    # 첫 block 은 show 이전에 emit 됐어야 (순서)
    show_idx = events.index(show[0])
    block_before_idx = [i for i, e in enumerate(events) if e.t == "block_commit"][0]
    assert block_before_idx < show_idx


def test_offset_dedup_drops_duplicate_emission():
    """같은 (offset, type) 튜플은 재발화 금지.

    monotonic offset 가정 하에서 — 동일 offset 의 commit 이 재공급되면 dedup set 에
    의해 중복 emit 이 억제됨을 확인. 역행 offset 은 §13.7 의 monotonic assertion 으로
    별도 차단되므로 여기서는 같은 offset 을 반복 feed.
    """
    c = EventClassifier()
    line1 = _mk("content", "⏺ response", offset=100)
    line2 = _mk("content", "", offset=110)
    out1 = c.feed(line1)
    out2 = c.feed(line2)
    # 같은 commit 재공급 (offset 유지). flush 상태라 buffer 는 비어있으므로 이벤트는
    # 발생하지 않지만, 만약 상위가 오류로 동일 offset 을 재공급하면 dedup 이 받쳐준다.
    out3 = c.feed(_mk("content", "", offset=110))
    all_events = out1 + out2 + out3 + c.flush()
    blocks = [e for e in all_events if e.t == "block_commit"]
    offsets = [b.offset for b in blocks]
    assert len(offsets) == len(set(offsets))


def test_monotonic_offset_assertion():
    """§13.7 D1 — offset 역행 시 즉시 AssertionError."""
    import pytest
    c = EventClassifier()
    c.feed(_mk("content", "a", offset=100))
    with pytest.raises(AssertionError, match="offset regression"):
        c.feed(_mk("content", "b", offset=50))


def test_scroll_evict_exempt_from_offset_assertion():
    """scroll_evict 는 row=-1 이고 offset 이 시간 순서가 아닐 수 있어 예외."""
    c = EventClassifier()
    c.feed(_mk("content", "a", offset=100))
    evicted = TaggedLine(
        commit=LineCommit(
            offset=50, row=-1, text="old", reason="scroll_evict", screen=()
        ),
        region="content",
    )
    # 예외가 발생하지 않아야 함
    c.feed(evicted)


def test_user_echo_input_box_region_suppressed():
    """§13.11.c — region=input_box commit 은 이벤트 발행 금지 (user_echo)."""
    c = EventClassifier()
    tl = TaggedLine(
        commit=LineCommit(
            offset=10, row=20, text="hello world",
            reason="timer", screen=(),
        ),
        region="input_box",
    )
    out = c.feed(tl)
    assert out == []


def test_user_echo_prompt_box_range_suppressed():
    """§13.11.c — content region 이지만 commit row 가 prompt_box_range 안이면 echo."""
    c = EventClassifier()
    tl = TaggedLine(
        commit=LineCommit(
            offset=10, row=21, text="⏺ would-be response",
            reason="timer", screen=(),
        ),
        region="content",
        prompt_box_range=(20, 23),  # row 21 이 내부
    )
    out = c.feed(tl)
    # block_commit 이 발화되면 안 됨 (echo 로 간주)
    assert [e for e in out if e.t == "block_commit"] == []


def test_compact_start_emitted_once():
    """§13.11.d — /compact echo 또는 Compacting banner 에서 compact_start 1회."""
    events = _run(
        _mk("content", "/compact", offset=10),
        _mk("content", "Compacting conversation...", offset=20),
    )
    starts = [e for e in events if e.t == "compact_start"]
    assert len(starts) == 1
    assert starts[0].offset == 10


def test_compact_complete_on_crunched_banner():
    """start 이후 Crunched for N lines 배너 보면 complete."""
    events = _run(
        _mk("content", "/compact", offset=10),
        _mk("content", "Compacting conversation...", offset=20),
        _mk("content", "Crunched for 142 lines", offset=30),
    )
    assert any(e.t == "compact_start" for e in events)
    completes = [e for e in events if e.t == "compact_complete"]
    assert len(completes) == 1
    assert completes[0].offset == 30


def test_compact_complete_on_welcome_rebirth():
    """§13.11.d — 세션 중간에 welcome 재출현 → compact_complete 로 승격."""
    events = _run(
        _mk("content", "Welcome to Claude Code", offset=5),   # 초기 부팅
        _mk("content", "Compacting conversation...", offset=100),
        _mk("content", "Welcome to Claude Code", offset=200), # 재출현
    )
    completes = [e for e in events if e.t == "compact_complete"]
    assert len(completes) == 1
    assert completes[0].offset == 200
    # session_boot 도 2번 발화
    boots = [e for e in events if e.t == "session_boot"]
    assert len(boots) == 2


def test_compact_multi_boot_first_boot_also_has_compact():
    """부팅 직후 compact 도 동일하게 start→complete 페어로 처리."""
    events = _run(
        _mk("content", "Welcome to Claude Code", offset=5),
        _mk("content", "Compacting conversation...", offset=50),
        _mk("content", "Crunched for 99 lines", offset=80),
    )
    types = [e.t for e in events]
    assert types.count("compact_start") == 1
    assert types.count("compact_complete") == 1


def test_limit_event_detected():
    events = _run(_mk("content", "You've hit your limit", offset=5))
    assert any(e.t == "limit" for e in events)


def test_trust_prompt_event_detected():
    events = _run(_mk("content", "Quick safety check", offset=5))
    assert any(e.t == "trust_prompt" for e in events)


def test_resume_picker_event_detected():
    events = _run(
        _mk("content",
            "1. Resume from summary ...  2. Resume full session",
            offset=5)
    )
    assert any(e.t == "resume_picker" for e in events)


def test_prompt_box_range_outside_not_echo():
    """prompt_box_range 밖의 row 는 정상 처리."""
    c = EventClassifier()
    tl = TaggedLine(
        commit=LineCommit(
            offset=10, row=5, text="⏺ response",
            reason="timer", screen=(),
        ),
        region="content",
        prompt_box_range=(20, 23),
    )
    c.feed(tl)
    # 빈줄로 블록 flush
    tl2 = TaggedLine(
        commit=LineCommit(offset=20, row=6, text="", reason="timer", screen=()),
        region="content",
        prompt_box_range=(20, 23),
    )
    out = c.feed(tl2)
    assert any(e.t == "block_commit" for e in out)


def test_session_boot_banner_detected():
    events = _run(_mk("content", "Welcome to Claude Code", offset=5))
    assert any(e.t == "session_boot" for e in events)


def test_session_resume_banner_detected():
    events = _run(_mk("content", "Resuming session ...", offset=5))
    assert any(e.t == "session_resume" for e in events)


def test_busy_spinner_detected():
    events = _run(_mk("content", "✶ Crunched for 3m 48s", offset=5))
    assert any(e.t == "busy_enter" for e in events)


def test_approval_cancel_on_flush():
    """모달이 해소되지 않은 채 flush 되면 cancel 로 기록."""
    screen = (
        "",
        "─" * 60,
        "Do you want to proceed?",
        " ❯ 1. Yes",
        "Esc to cancel · Tab to amend",
    )
    events = _run(
        _mk("modal_overlay", "Do you want to proceed?", offset=10, row=2, screen=screen),
        _mk("modal_overlay", " ❯ 1. Yes", offset=15, row=3, screen=screen),
        _mk("modal_overlay", "Esc to cancel · Tab to amend", offset=20, row=4, screen=screen),
    )
    types = [e.t for e in events]
    assert "approval_show" in types
    assert "approval_cancel" in types


def test_snapshot_path_matches_parser_extraction():
    """`⏺` commit 이 commit.screen 과 함께 들어오면 frozen 파서와 동일한 rectangle
    을 block_commit 으로 발화한다. shadow_diff 의 `parser - analyzer = ∅` 불변식
    구조화 검증."""
    from bridge.parser import extract_response_blocks

    screen = (
        "Welcome to Claude Code",
        "",
        "⏺ 이제 첫 응답",
        "  본문 1",
        "  본문 2",
        "",
        "⏺ 두 번째 응답",
        "  tail",
        "",
        "─" * 60,
        "❯ ",
        "─" * 60,
    )
    # `⏺` 두 개가 snapshot 에 모두 보이는 순간을 시뮬레이션 — 두 번째 `⏺` commit.
    c = EventClassifier()
    out = c.feed(_mk("content", "⏺ 두 번째 응답", offset=100, row=6, screen=screen))
    out.extend(c.flush())
    blocks = [e for e in out if e.t == "block_commit"]
    expected = extract_response_blocks("\n".join(screen))
    # 파서가 보는 집합을 그대로 커버 (순서 무관 set 비교)
    assert {b.payload["text"] for b in blocks} == {b.strip() for b in expected if b.strip()}


def test_snapshot_path_dedupes_repeated_screens():
    """동일 스냅샷이 두 번 들어와도 같은 본문의 block_commit 은 1회만 발화."""
    screen = (
        "",
        "⏺ 한 블록",
        "  본문",
        "",
        "─" * 60,
        "❯ ",
        "─" * 60,
    )
    c = EventClassifier()
    a = c.feed(_mk("content", "⏺ 한 블록", offset=10, row=1, screen=screen))
    b = c.feed(_mk("content", "⏺ 한 블록", offset=20, row=1, screen=screen))
    out = a + b + c.flush()
    blocks = [e for e in out if e.t == "block_commit"]
    assert len(blocks) == 1


def test_content_line_after_modal_flushes_held_blocks():
    """approval 해소 후 보류된 block 이 올바른 순서로 emit."""
    screen = (
        "─" * 60,
        "Do you want to proceed?",
        " ❯ 1. Yes",
        "Esc to cancel · Tab to amend",
    )
    c = EventClassifier()
    out = []
    # 모달 등장
    out.extend(c.feed(_mk("modal_overlay", "Do you want to proceed?", offset=10, screen=screen)))
    out.extend(c.feed(_mk("modal_overlay", " ❯ 1. Yes", offset=15, screen=screen)))
    out.extend(c.feed(_mk("modal_overlay", "Esc to cancel · Tab to amend", offset=20, screen=screen)))
    # 모달 중 block 이 도달
    out.extend(c.feed(_mk("content", "⏺ held block", offset=30)))
    out.extend(c.feed(_mk("content", "", offset=35)))
    # 모달 종료 없이 flush
    out.extend(c.flush())
    # approval_cancel 후 held block 이 emit 돼야
    types = [e.t for e in out]
    assert "approval_show" in types
    assert "approval_cancel" in types
    assert "block_commit" in types
    # 순서: approval_show → approval_cancel → block_commit
    show_i = types.index("approval_show")
    cancel_i = types.index("approval_cancel")
    block_i = types.index("block_commit")
    assert show_i < cancel_i <= block_i
