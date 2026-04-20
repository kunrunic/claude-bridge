"""region tagger 테스트 — step-2-spec [4]."""
from __future__ import annotations

from tools.line_commit import LineCommit
from tools.region_tagger import RegionTagger


def _tag(tagger: RegionTagger, texts: list[str], *, in_alt: bool = False) -> list[str]:
    out = []
    for i, t in enumerate(texts):
        c = LineCommit(offset=i, row=i, text=t, reason="timer", in_alt=in_alt)
        out.append(tagger.tag(c).region)
    return out


def test_plain_content_is_content():
    t = RegionTagger()
    regions = _tag(t, ["⏺ response text", "continuation line"])
    assert regions == ["content", "content"]


def test_input_box_header_outside_modal():
    t = RegionTagger()
    regions = _tag(t, ["⏺ prior response", "❯ user typing"])
    assert regions[-1] == "input_box"


def test_modal_detected_between_dividers():
    t = RegionTagger()
    regions = _tag(
        t,
        [
            "⏺ response",
            "─" * 80,  # opening divider
            "Do you want to make this edit to x.py?",
            "❯ 1. Yes",
            "  2. Yes, allow all",
            "  3. No",
            "Esc to cancel · Tab to amend",
            "─" * 80,  # closing divider
            "⏺ next response",
        ],
    )
    # modal 인식
    modal_count = sum(1 for r in regions if r == "modal_overlay")
    assert modal_count >= 5  # body + 3 choices + footer at minimum
    # 마지막 라인은 다시 content
    assert regions[-1] == "content"


def test_alt_screen_always_modal():
    t = RegionTagger()
    regions = _tag(t, ["some modal text", "another"], in_alt=True)
    assert regions == ["modal_overlay", "modal_overlay"]


def test_bypass_permissions_is_chrome():
    t = RegionTagger()
    regions = _tag(t, ["⏵⏵ bypass permissions on (shift+tab to cycle)"])
    assert regions == ["chrome"]


def test_scroll_evicted_is_content():
    t = RegionTagger()
    c = LineCommit(offset=0, row=-1, text="evicted line", reason="scroll_evict")
    assert t.tag(c).region == "content"


def test_esc_to_cancel_inside_modal_is_modal():
    t = RegionTagger()
    regions = _tag(
        t,
        [
            "─" * 80,
            "Do you want to proceed?",
            "❯ 1. Yes",
            "Esc to cancel · Tab to amend",
        ],
    )
    # Esc to cancel 은 modal footer
    assert regions[-1] == "modal_overlay"


def test_divider_without_modal_body_stays_content():
    t = RegionTagger()
    regions = _tag(
        t,
        [
            "⏺ header",
            "─" * 80,  # decorative divider, no modal body follows
            "⏺ continuation",
            "more content",
        ],
    )
    # divider 자체는 content (이전 모달 없었으므로)
    assert regions[1] == "content"
    assert regions[2] == "content"
