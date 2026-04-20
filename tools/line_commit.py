"""Line-commit 검출기 — step-2-spec [3].

token 스트림 → VTScreen 반영 → 특정 row 의 내용이 "확정" 됐다고 판정되는 순간
LineCommit 이벤트 발화.

규칙 (R1 + R2 + R3 합):
- **R1 (cursor-leaves-and-stays)**: row R 이 dirty 됐다가 cursor 가 떠난 후 K 토큰
  동안 R 이 다시 dirty 되지 않으면 commit.
- **R2 (LF on scroll bottom)**: scroll 에 의해 top row 가 화면 밖으로 밀려나는
  순간 (VTScreen.evicted_lines 로 감지) 그 내용을 commit.
- **EOF flush**: pending 모두 commit.

K 는 케이스별 튜닝 — default 64. case-04 keyframe 3 tick 에서 의도한 commit set 을
얻도록 조정.

**Dedup**: 같은 row 에서 이전에 commit 된 텍스트와 동일하면 skip. region 태거가
위에서 다시 dedup 하므로 여기선 단순히 txt 동일 비교.
"""
from __future__ import annotations

from dataclasses import dataclass

from tools.ansi_tokenizer import Token
from tools.vt_screen import VTScreen


@dataclass
class LineCommit:
    offset: int          # 이 commit 을 유발한 token 의 offset_end
    row: int             # VT grid 상의 row index (in_alt 면 alt grid row)
    text: str            # 커밋 시점의 row 내용 (rstrip space)
    reason: str          # "timer" | "scroll_evict" | "flush"
    in_alt: bool = False # alt-screen 중 커밋 여부 (modal_overlay hint)
    screen: tuple[str, ...] = ()  # commit 시점의 VT 전체 스냅샷 (region 태거용)


class LineCommitter:
    """VTScreen 을 감싸면서 LineCommit 을 yield."""

    def __init__(self, *, rows: int = 24, cols: int = 200, K: int = 64) -> None:
        self.vt = VTScreen(rows=rows, cols=cols)
        self.K = K
        self.token_idx = 0
        # row -> (last_dirty_token_idx, last_text, last_offset, in_alt)
        self._pending: dict[tuple[bool, int], tuple[int, str, int]] = {}
        # row -> last committed text (dedup)
        self._last_committed: dict[tuple[bool, int], str] = {}

    def feed(self, tok: Token) -> list[LineCommit]:
        self.vt.consume(tok)
        self.token_idx += 1

        out: list[LineCommit] = []

        # R2: scroll-evicted lines
        if self.vt.evicted_lines:
            for text in self.vt.evicted_lines:
                if text.strip():
                    key = (False, -1)  # row -1 for evicted
                    if self._last_committed.get(key) != text:
                        out.append(
                            LineCommit(
                                offset=tok.offset_end,
                                row=-1,
                                text=text,
                                reason="scroll_evict",
                                in_alt=False,
                                screen=tuple(self.vt.snapshot()),
                            )
                        )
                        # don't record in _last_committed by row=-1 (multiple
                        # different lines can be evicted)
            self.vt.evicted_lines.clear()

        # R1: update pending from dirty rows
        in_alt = self.vt.in_alt_screen
        for r in self.vt.dirty_rows:
            text = self.vt.line(r)
            key = (in_alt, r)
            self._pending[key] = (self.token_idx, text, tok.offset_end)
        self.vt.clear_dirty()

        # R1: emit commits for rows where cursor left and K tokens passed.
        # 같은 batch 에서 expire 된 여러 row 는 **row 오름차순** 으로 commit —
        # 그래야 region 태거가 위→아래 순으로 divider/modal-body 를 관찰하여
        # 모달 영역을 정확히 인식 (Claude Code 가 bottom-up 으로 그려도 반영).
        cursor_r, _ = self.vt.cursor
        stale: list[tuple[bool, int]] = []
        for key, (last_idx, text, _) in self._pending.items():
            key_in_alt, key_row = key
            if key_in_alt == in_alt and key_row == cursor_r:
                continue
            if self.token_idx - last_idx >= self.K:
                stale.append(key)
        stale.sort(key=lambda k: (k[0], k[1]))
        for key in stale:
            _, text, offset = self._pending.pop(key)
            key_in_alt, row = key
            if not text.strip():
                continue
            if self._last_committed.get(key) == text:
                continue
            self._last_committed[key] = text
            out.append(
                LineCommit(
                    offset=offset,
                    row=row,
                    text=text,
                    reason="timer",
                    in_alt=key_in_alt,
                    screen=tuple(self.vt.snapshot()),
                )
            )
        return out

    def flush(self) -> list[LineCommit]:
        """EOF — pending 모두 commit."""
        out: list[LineCommit] = []
        for key, (_, text, offset) in sorted(self._pending.items()):
            if not text.strip():
                continue
            if self._last_committed.get(key) == text:
                continue
            key_in_alt, row = key
            self._last_committed[key] = text
            out.append(
                LineCommit(
                    offset=offset,
                    row=row,
                    text=text,
                    reason="flush",
                    in_alt=key_in_alt,
                    screen=tuple(self.vt.snapshot()),
                )
            )
        self._pending.clear()
        return out
