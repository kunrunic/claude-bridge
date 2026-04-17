"""20260418_010300 회귀 방지 — fresh 세션에서 ⏺ 응답 누락.

재연결(post_init) 직후 CB2 의 fresh/short pane 에는 다음 구조가 떠 있다:

    ▐▛███▜▌   Claude Code v2.1.112    ← 부팅 배너 (line 0)
    ▝▜█████▛▘  Opus 4.7 · Claude Max
      ▘▘ ▝▝    ~/claude-bridge2

    ❯ hi?

    ⏺ Hi! How can I help you today?

    ──────────── (80 dashes, divider)
    ❯
    ──────────── (80 dashes)
      ? for shortcuts

pane 이 짧아 `_response_region` 의 "마지막 15줄" 스캔이 pane 전체를 덮는다.
스캔 중 line 0 의 "Claude Code v2.1.112" 가 restart 배너로 오인되어
`end = 0` 으로 설정 → `extract_response_blocks` 가 빈 리스트 반환 →
`⏺ Hi!` 응답이 사용자에게 전달되지 않는 사고.

해결: 배너가 pane 맨 위(위쪽에 non-blank 내용 없음)에 있으면 초기 부팅
배너로 간주하고 `end` 경계로 쓰지 않는다. 실제 restart-in-place 는 배너
위에 이전 세션 내용이 남아 있으므로 구별된다.
"""
from __future__ import annotations

from bridge import parser


FRESH_PANE = """\
 ▐▛███▜▌   Claude Code v2.1.112
▝▜█████▛▘  Opus 4.7 · Claude Max
  ▘▘ ▝▝    ~/claude-bridge2

❯ hi?

⏺ Hi! How can I help you today?

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts
"""


RESTART_PANE = """\
  (이전 세션 대화 내용 어쩌구)
  ⏺ 이전 응답 블록
  (더 많은 내용)

 ▐▛███▜▌   Claude Code v2.1.112
▝▜█████▛▘  Opus 4.7 · Claude Max
  ▘▘ ▝▝    ~/claude-bridge2

❯ 재시작후
⏺ 재시작 이후 응답

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts
"""


def test_fresh_pane_yields_response_block():
    """부팅 배너가 pane 맨 위에 있는 짧은 pane 에서도 ⏺ 블록을 찾아낸다."""
    blocks = parser.extract_response_blocks(FRESH_PANE)
    assert len(blocks) == 1, f"expected 1 block, got {len(blocks)}: {blocks}"
    assert "Hi! How can I help you today?" in blocks[0]


def test_restart_banner_still_truncates_response_region():
    """배너가 pane 중간(위에 이전 내용 존재)에 있으면 여전히 end 경계로 쓴다.

    기존 semantic 유지 — restart 배너는 scrollback 경계로 작동해, 배너 아래쪽
    신규 내용은 응답 블록으로 보지 않는다 (사용자는 다음 interaction 에서
    자연스럽게 배너를 넘긴 상태의 pane 을 보게 됨). 본 테스트는 회귀 방지를
    위한 behavior pin — 새 패치가 배너를 완전히 무시해 pre-restart 내용을
    누락시키지 않도록."""
    lines, end = parser._response_region(RESTART_PANE)
    # end 는 pane 중간 어딘가 (배너/divider 중 더 작은 값) — 0 도 아니고 end-of-pane 도 아니어야 함
    assert 0 < end < len(lines), f"end 가 비정상 범위: {end} (총 {len(lines)}줄)"


def test_response_region_end_not_clamped_to_zero_on_top_banner():
    """regression: _response_region 이 top-banner 를 만나 end=0 이 되지 않음."""
    lines, end = parser._response_region(FRESH_PANE)
    assert end > 0, f"end was clamped to {end} by top banner — ⏺ 블록을 못 봄"


def test_response_region_ignores_blank_lines_above_initial_banner():
    """pane 앞에 blank 라인만 있고 배너가 line 3 정도에 있어도 초기 배너로 간주."""
    pane = "\n\n\n" + FRESH_PANE  # 배너 앞에 빈 줄 3개
    blocks = parser.extract_response_blocks(pane)
    assert len(blocks) == 1
    assert "Hi!" in blocks[0]
