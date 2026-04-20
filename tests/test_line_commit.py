"""line-commit 검출기 단위 테스트 — step-2-spec [3]."""
from __future__ import annotations

from tools.ansi_tokenizer import tokenize_bytes
from tools.line_commit import LineCommitter


def _feed_all(committer: LineCommitter, data: bytes) -> list:
    out = []
    for tok in tokenize_bytes(data):
        out.extend(committer.feed(tok))
    out.extend(committer.flush())
    return out


def test_single_line_commits_on_flush():
    lc = LineCommitter(K=64)
    commits = _feed_all(lc, b"hello")
    assert len(commits) == 1
    assert commits[0].text == "hello"
    assert commits[0].reason == "flush"


def test_cursor_leaves_and_timer_commits():
    lc = LineCommitter(K=4)
    # 짧은 K 로 타이머 commit 유도: row0 쓰고 row1 로 간 뒤 6 토큰 더
    commits = _feed_all(lc, b"line0\r\nxxxxxxxx")
    # line0 은 타이머로 커밋, line1 은 flush 로 커밋
    texts = [c.text for c in commits]
    assert "line0" in texts
    assert "xxxxxxxx" in texts


def test_multiple_lines_commit_in_order():
    lc = LineCommitter(K=64)
    commits = _feed_all(lc, b"a\r\nb\r\nc\r\n")
    assert [c.text for c in commits] == ["a", "b", "c"]


def test_dedup_same_text_on_row():
    lc = LineCommitter(K=2)
    # row0 "aa" → commit, 다시 같은 "aa" 쓰면 dedup
    commits: list = []
    for tok in tokenize_bytes(b"aa\r\n\x1b[Haa\r\nxxxx"):
        commits.extend(lc.feed(tok))
    commits.extend(lc.flush())
    aa_count = sum(1 for c in commits if c.text == "aa")
    assert aa_count == 1


def test_scroll_evict_commits_top_line():
    # rows=3: 3 줄 채우고 4번째 LF 에서 top 이 밀려남
    lc = LineCommitter(rows=3, cols=10, K=1000)
    commits = _feed_all(lc, b"a\r\nb\r\nc\r\nd\r\ne")
    # 첫 LF 에서 a 밀려나는 건 아님 — a 가 row0, b 가 row1, c 가 row2. LF 에서
    # cursor 가 row2 에서 row3 로 갈 때 scroll → row0 ("a") 밀림.
    texts = [c.text for c in commits]
    assert "a" in texts
    # reason 확인
    reasons = [c.reason for c in commits if c.text == "a"]
    assert "scroll_evict" in reasons


def test_alt_screen_rows_tagged():
    lc = LineCommitter(K=2)
    commits = _feed_all(
        lc,
        b"\x1b[?1049hmodal\r\n" + (b"x" * 10),
    )
    modal_commits = [c for c in commits if c.in_alt]
    assert any(c.text == "modal" for c in modal_commits)


def test_cursor_returns_to_row_resets_timer():
    """cursor 가 row 떠난 뒤 K 전에 다시 돌아오면 commit 지연/취소."""
    lc = LineCommitter(K=5)
    commits: list = []
    # row0 "hi" → row1 로 이동 → 3 tok 후 다시 row0 복귀
    for tok in tokenize_bytes(b"hi\r\nxx\x1b[Hre"):
        commits.extend(lc.feed(tok))
    commits.extend(lc.flush())
    # row0 의 최종 상태는 "rei" (over-write 로 "re" + old "i") — "hi" 가 아니라
    # 새 문자열로 한 번 더 commit 돼야. dedup 은 row 별.
    row0_commits = [c for c in commits if c.row == 0]
    # 최종 row0 내용이 "hi" 가 아니어야 한다 (overwrite 된 상태)
    assert len(row0_commits) >= 1
    final_text = row0_commits[-1].text
    assert final_text.startswith("re")


def test_case_04_raw_smoke():
    from pathlib import Path
    raw = Path("tools/fixtures/baseline/case-04-edit-approval-wording/pipe-pane-raw.log")
    if not raw.is_file():
        import pytest
        pytest.skip("baseline fixture not present")
    data = raw.read_bytes()[:20000]
    lc = LineCommitter()
    commits = _feed_all(lc, data)
    # 최소한 몇 개 commit 이 나와야
    assert len(commits) > 0
    # 승인 다이얼로그 문구가 있어야 (첫 20KB 범위 안에 있을 가능성)
    # 또는 ⏺ 블록 중 하나라도 있어야
    texts = [c.text for c in commits]
    assert any("⏺" in t or "Do you want to" in t or "❯" in t for t in texts), \
        f"expected at least one TUI marker in commits: {texts[:5]}"
