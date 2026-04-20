"""ANSI 토크나이저 단위 테스트.

대상: tools/ansi_tokenizer.py (step-2-spec [1]).
"""
from __future__ import annotations

from pathlib import Path

from tools.ansi_tokenizer import Token, Tokenizer, tokenize_bytes


def _kinds(tokens: list[Token]) -> list[str]:
    return [t.kind for t in tokens]


def test_incomplete_csi_across_chunks_counted_and_recovered():
    """§13.4 — chunk 경계에서 CSI 가 잘려도 drop 금지 + 카운터 증가."""
    tok = Tokenizer()
    # chunk1: ESC[3 (incomplete)
    out1 = list(tok.feed(0, b"\x1b[3"))
    assert tok.incomplete_escape_count >= 1
    # 아직 완결 안 됐으므로 csi token 없음
    assert not any(t.kind == "csi" for t in out1)
    # chunk2: 나머지 `1m` 도착
    out2 = list(tok.feed(3, b"1mX"))
    assert any(t.kind == "csi" for t in out2)
    texts = [t.payload for t in out2 if t.kind == "text"]
    assert "X" in texts


def test_incomplete_escape_at_flush_counted_separately():
    """flush 시 미완결 ESC 는 `other` 로 배출 + incomplete_escape_flushed 증가."""
    tok = Tokenizer()
    list(tok.feed(0, b"\x1b[3"))
    flushed = list(tok.flush())
    assert tok.incomplete_escape_flushed == 1
    assert any(t.kind == "other" for t in flushed)


def test_incomplete_utf8_across_chunks_counted():
    """UTF-8 멀티바이트가 잘려도 buffer 유지 + 카운터 증가."""
    tok = Tokenizer()
    # "가" = e1 b0 80 (3 bytes)
    data = "가".encode("utf-8")
    out1 = list(tok.feed(0, data[:2]))  # 첫 2 bytes 만
    # 아직 character 불완전
    assert not any(t.kind == "text" and "가" in t.payload for t in out1)
    assert tok.incomplete_escape_count >= 1
    out2 = list(tok.feed(2, data[2:3]))
    texts = [t.payload for t in out2 if t.kind == "text"]
    assert "가" in texts


def test_plain_text():
    toks = tokenize_bytes(b"hello")
    assert len(toks) == 1
    assert toks[0].kind == "text"
    assert toks[0].payload == "hello"
    assert (toks[0].offset_start, toks[0].offset_end) == (0, 5)


def test_c0_controls_split_text():
    toks = tokenize_bytes(b"a\nb\rc")
    assert _kinds(toks) == ["text", "lf", "text", "cr", "text"]
    assert toks[0].payload == "a"
    assert toks[2].payload == "b"
    assert toks[4].payload == "c"


def test_csi_sgr_color():
    # ESC [ 31 m  →  red
    toks = tokenize_bytes(b"\x1b[31mred\x1b[0m")
    assert _kinds(toks) == ["csi", "text", "csi"]
    assert toks[0].payload == {"params": "31", "final": "m"}
    assert toks[1].payload == "red"
    assert toks[2].payload == {"params": "0", "final": "m"}


def test_csi_cursor_position():
    # ESC [ 5;10 H
    toks = tokenize_bytes(b"\x1b[5;10H")
    assert toks == [Token(0, 7, "csi", {"params": "5;10", "final": "H"})]


def test_alt_screen_promoted():
    toks = tokenize_bytes(b"\x1b[?1049h\x1b[?1049l")
    assert _kinds(toks) == ["alt_screen_on", "alt_screen_off"]
    assert toks[0].payload is None


def test_osc_title_bel_terminated():
    toks = tokenize_bytes(b"\x1b]0;my title\x07tail")
    assert _kinds(toks) == ["osc", "text"]
    assert toks[0].payload == "0;my title"
    assert toks[1].payload == "tail"


def test_osc_st_terminated():
    toks = tokenize_bytes(b"\x1b]1337;foo\x1b\\bar")
    assert _kinds(toks) == ["osc", "text"]
    assert toks[0].payload == "1337;foo"


def test_chunk_boundary_inside_csi():
    """CSI 가 chunk 경계를 가로질러도 정상 tokenize."""
    tok = Tokenizer()
    out: list[Token] = []
    out.extend(tok.feed(0, b"a\x1b[3"))
    out.extend(tok.feed(4, b"1mred"))
    out.extend(tok.flush())
    assert _kinds(out) == ["text", "csi", "text"]
    assert out[1].payload == {"params": "31", "final": "m"}


def test_chunk_boundary_inside_utf8():
    """UTF-8 멀티바이트 경계에서 partial 이 pending 으로 유지."""
    # '한' = 0xED 0x95 0x9C — split after first byte
    tok = Tokenizer()
    out: list[Token] = []
    out.extend(tok.feed(0, b"a\xed"))
    out.extend(tok.feed(2, b"\x95\x9Cb"))
    out.extend(tok.flush())
    texts = [t.payload for t in out if t.kind == "text"]
    assert "".join(texts) == "a한b"


def test_unicode_box_drawing_is_text():
    toks = tokenize_bytes("╭─╮│╰─╯".encode("utf-8"))
    assert len(toks) == 1 and toks[0].kind == "text"
    assert toks[0].payload == "╭─╮│╰─╯"


def test_case_04_raw_first_tokens_smoke():
    """baseline case-04 raw 첫 4KB 가 크래시 없이 tokenize. 정확한 결과 검증은
    Step 2 후반부에서 fixture snapshot 으로."""
    raw = Path("tools/fixtures/baseline/case-04-edit-approval-wording/pipe-pane-raw.log")
    if not raw.is_file():
        # CI 환경에서 심볼릭 링크 없으면 skip — 로컬 개발 target
        import pytest
        pytest.skip("baseline fixture not present")
    data = raw.read_bytes()[:4096]
    toks = tokenize_bytes(data)
    # 최소한 기본 token kind 가 골고루 나와야
    kinds = set(_kinds(toks))
    assert "text" in kinds
    assert "csi" in kinds
    # offset 연속성 (앞 token 의 end == 뒤 token 의 start)
    for a, b in zip(toks, toks[1:]):
        assert a.offset_end == b.offset_start


def test_incomplete_csi_at_eof_becomes_other():
    toks = tokenize_bytes(b"a\x1b[31")
    assert toks[0].payload == "a"
    # final 에서 incomplete CSI 는 other
    assert toks[-1].kind == "other"
