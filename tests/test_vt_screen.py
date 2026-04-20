"""VT 가상 스크린 단위 테스트 — step-2-spec [2]."""
from __future__ import annotations

from tools.ansi_tokenizer import tokenize_bytes
from tools.vt_screen import VTScreen


def _feed(vt: VTScreen, data: bytes) -> None:
    vt.consume_many(tokenize_bytes(data))


def test_plain_text_writes_and_advances_cursor():
    vt = VTScreen(rows=5, cols=20)
    _feed(vt, b"hello")
    assert vt.line(0) == "hello"
    assert vt.cursor == (0, 5)


def test_lf_moves_cursor_down():
    vt = VTScreen(rows=5, cols=20)
    _feed(vt, b"a\nb")
    # a → (0,1); LF keeps col → (1,1); b → (1,2). LF 는 col 유지 (raw 모드)
    assert vt.cursor == (1, 2)
    assert vt.line(0) == "a"
    assert vt.line(1) == " b"


def test_crlf_combo_resets_col():
    vt = VTScreen(rows=5, cols=20)
    _feed(vt, b"abc\r\nxyz")
    assert vt.line(0) == "abc"
    assert vt.line(1) == "xyz"
    assert vt.cursor == (1, 3)


def test_cursor_home_and_overwrite():
    vt = VTScreen(rows=5, cols=20)
    _feed(vt, b"abcdef\x1b[Hxy")
    assert vt.line(0) == "xycdef"
    assert vt.cursor == (0, 2)


def test_erase_display_2():
    vt = VTScreen(rows=3, cols=10)
    _feed(vt, b"abc\r\ndef\r\nghi")
    _feed(vt, b"\x1b[2J\x1b[H")
    assert all(vt.line(r) == "" for r in range(3))
    assert vt.cursor == (0, 0)


def test_erase_line_0_from_cursor():
    vt = VTScreen(rows=2, cols=20)
    _feed(vt, b"hello world")
    # position col=5 (1-based) → col 4. erase K mode 0: from col 4 to end
    _feed(vt, b"\x1b[5G\x1b[K")
    assert vt.line(0) == "hell"


def test_alt_screen_isolates_primary():
    vt = VTScreen(rows=3, cols=10)
    _feed(vt, b"primary")
    assert vt.line(0) == "primary"
    _feed(vt, b"\x1b[?1049h")
    assert vt.in_alt_screen is True
    # alt is blank
    assert vt.line(0) == ""
    _feed(vt, b"modal")
    assert vt.line(0) == "modal"
    _feed(vt, b"\x1b[?1049l")
    assert vt.in_alt_screen is False
    # primary restored
    assert vt.line(0) == "primary"


def test_scroll_region_contains_scrolling():
    vt = VTScreen(rows=5, cols=10)
    # set scroll region rows 2..4 (1-based, so 1..3 0-based)
    _feed(vt, b"top\r\n")  # row 0
    _feed(vt, b"\x1b[2;4r")  # region rows 1..3 → cursor to (0,0)
    _feed(vt, b"\x1b[2;1Ha\r\nb\r\nc\r\nd")  # writes within region, scrolls
    # row 0 still "top" because it's outside the scroll region
    assert vt.line(0) == "top"
    # after scroll, content shifted
    assert vt.line(3) == "d"


def test_cursor_up_down_clamped():
    vt = VTScreen(rows=3, cols=10)
    _feed(vt, b"\x1b[100B")  # try to go down 100
    assert vt.cursor[0] == 2  # clamped to last row
    _feed(vt, b"\x1b[100A")
    assert vt.cursor[0] == 0


def test_sgr_changes_attrs_not_chars():
    vt = VTScreen(rows=1, cols=10)
    _feed(vt, b"\x1b[31mred\x1b[0m")
    assert vt.line(0) == "red"


def test_dirty_rows_tracked():
    vt = VTScreen(rows=3, cols=10)
    _feed(vt, b"abc")
    assert 0 in vt.dirty_rows
    vt.clear_dirty()
    _feed(vt, b"\r\nx")
    assert 1 in vt.dirty_rows
    assert 0 not in vt.dirty_rows


def test_case_04_raw_smoke_through_vt():
    """case-04 raw 앞 8KB 가 크래시 없이 VT 상태에 흘러들어감."""
    from pathlib import Path
    raw = Path("tools/fixtures/baseline/case-04-edit-approval-wording/pipe-pane-raw.log")
    if not raw.is_file():
        import pytest
        pytest.skip("baseline fixture not present")
    data = raw.read_bytes()[:8192]
    vt = VTScreen()
    vt.consume_many(tokenize_bytes(data))
    snap = vt.snapshot()
    assert any(s for s in snap), "expected at least one non-empty line"
