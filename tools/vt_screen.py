"""VT 가상 스크린 — step-2-spec [2].

Token 스트림을 받아 24행 × 200열 그리드에 렌더. cursor, erase, scroll region,
alt-screen 분리. SGR 는 기록만 (rendering 결정에 사용 안 함).

설계 원칙:
- **읽기 쉬운 state**: grid[row][col] 에 cell = (char, attrs). 최소 PoC 사양.
- **alt-screen 분리**: enter 시 primary 저장 / leave 시 primary 복원.
- **scrollback 없음**: 위로 넘친 줄은 버린다 (우리의 scrollback 은 pipe-pane 파일).
- **dirty line 추적**: 각 tick 에 바뀐 row 를 set 으로 노출 — line-commit 검출기가
  이를 이용.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from tools.ansi_tokenizer import Token


@dataclass
class Cell:
    ch: str = " "
    # SGR attrs stored as raw params string for now — 분석기는 거의 사용 안 함
    attrs: str = ""


@dataclass
class _ScreenBuffer:
    rows: int
    cols: int
    grid: list[list[Cell]] = field(default_factory=list)
    cursor_row: int = 0
    cursor_col: int = 0
    scroll_top: int = 0  # inclusive
    scroll_bottom: int = 0  # inclusive, init to rows-1
    saved_cursor: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        self.grid = [[Cell() for _ in range(self.cols)] for _ in range(self.rows)]
        self.scroll_bottom = self.rows - 1


class VTScreen:
    """ANSI Token 을 소비해 가상 스크린 상태를 유지한다.

    사용법:
        vt = VTScreen()
        for tok in tokenizer.feed(...):
            vt.consume(tok)
            # vt.dirty_rows 로 변경 감지, vt.line(r) 로 내용 조회
    """

    def __init__(self, rows: int = 24, cols: int = 200) -> None:
        self.rows = rows
        self.cols = cols
        self._primary = _ScreenBuffer(rows, cols)
        self._alt: _ScreenBuffer | None = None
        self._in_alt = False
        self._sgr_attrs = ""
        self.dirty_rows: set[int] = set()
        # line-commit 검출기가 쓰는 힌트 — 실제 commit 은 검출기 쪽에서 결정
        self.cursor_left_row: int | None = None  # 직전에 커서가 떠난 row
        # scroll 로 grid 밖으로 밀려난 line 내용 (검출기가 drain).
        # consume() 사이에 누적됐다가 외부가 소비하면 clear.
        self.evicted_lines: list[str] = []

    # ---------------- public accessors ----------------

    @property
    def buf(self) -> _ScreenBuffer:
        if self._in_alt and self._alt is not None:
            return self._alt
        return self._primary

    @property
    def in_alt_screen(self) -> bool:
        return self._in_alt

    @property
    def cursor(self) -> tuple[int, int]:
        return (self.buf.cursor_row, self.buf.cursor_col)

    def line(self, row: int) -> str:
        """row 의 현재 내용 (trailing space 제거)."""
        if row < 0 or row >= self.rows:
            return ""
        cells = self.buf.grid[row]
        s = "".join(c.ch for c in cells)
        return s.rstrip(" ")

    def snapshot(self) -> list[str]:
        return [self.line(r) for r in range(self.rows)]

    def clear_dirty(self) -> None:
        self.dirty_rows.clear()
        self.cursor_left_row = None

    # ---------------- token consumption ----------------

    def consume(self, tok: Token) -> None:
        kind = tok.kind
        if kind == "text":
            assert isinstance(tok.payload, str)
            self._write_text(tok.payload)
        elif kind == "cr":
            self._set_cursor(self.buf.cursor_row, 0)
        elif kind == "lf":
            self._line_feed()
        elif kind == "bs":
            r, c = self.cursor
            if c > 0:
                self._set_cursor(r, c - 1)
        elif kind == "ht":
            # 단순 8-col tab (PoC 충분)
            r, c = self.cursor
            self._set_cursor(r, min(self.cols - 1, ((c // 8) + 1) * 8))
        elif kind == "alt_screen_on":
            self._enter_alt()
        elif kind == "alt_screen_off":
            self._leave_alt()
        elif kind == "csi":
            assert isinstance(tok.payload, dict)
            self._handle_csi(tok.payload["params"], tok.payload["final"])
        elif kind in ("osc", "other"):
            pass  # title 등 — 화면에 영향 없음
        else:  # pragma: no cover
            raise AssertionError(f"unknown token kind: {kind}")

    def consume_many(self, tokens: Iterable[Token]) -> None:
        for t in tokens:
            self.consume(t)

    # ---------------- internals ----------------

    def _write_text(self, text: str) -> None:
        buf = self.buf
        for ch in text:
            # autowrap: 마지막 col 도달 시 다음 줄로
            if buf.cursor_col >= self.cols:
                self._line_feed()
                buf.cursor_col = 0
            buf.grid[buf.cursor_row][buf.cursor_col] = Cell(ch=ch, attrs=self._sgr_attrs)
            self.dirty_rows.add(buf.cursor_row)
            buf.cursor_col += 1

    def _line_feed(self) -> None:
        buf = self.buf
        prev_row = buf.cursor_row
        if buf.cursor_row == buf.scroll_bottom:
            self._scroll_up()
        else:
            buf.cursor_row += 1
        if buf.cursor_row != prev_row:
            self.cursor_left_row = prev_row

    def _scroll_up(self) -> None:
        buf = self.buf
        # scroll region 내부에서 위로 1줄. 위로 밀려나는 top line 을 evict 기록.
        top, bot = buf.scroll_top, buf.scroll_bottom
        evicted = "".join(c.ch for c in buf.grid[top]).rstrip(" ")
        if evicted and not self._in_alt:
            self.evicted_lines.append(evicted)
        for r in range(top, bot):
            buf.grid[r] = buf.grid[r + 1]
            self.dirty_rows.add(r)
        buf.grid[bot] = [Cell() for _ in range(self.cols)]
        self.dirty_rows.add(bot)

    def _set_cursor(self, r: int, c: int) -> None:
        buf = self.buf
        new_r = max(0, min(self.rows - 1, r))
        new_c = max(0, min(self.cols - 1, c))
        if new_r != buf.cursor_row:
            self.cursor_left_row = buf.cursor_row
        buf.cursor_row = new_r
        buf.cursor_col = new_c

    def _handle_csi(self, params: str, final: str) -> None:
        # '?' 로 시작하는 private modes 는 alt-screen 외에는 대부분 ignore
        private = params.startswith("?")
        if private:
            body = params[1:]
        else:
            body = params
        nums = _parse_params(body)

        buf = self.buf
        r, c = buf.cursor_row, buf.cursor_col

        if final == "A":  # cursor up
            n = nums[0] if nums else 1
            self._set_cursor(r - n, c)
        elif final == "B":  # cursor down
            n = nums[0] if nums else 1
            self._set_cursor(r + n, c)
        elif final == "C":  # cursor forward
            n = nums[0] if nums else 1
            self._set_cursor(r, c + n)
        elif final == "D":  # cursor back
            n = nums[0] if nums else 1
            self._set_cursor(r, c - n)
        elif final == "E":  # next line
            n = nums[0] if nums else 1
            self._set_cursor(r + n, 0)
        elif final == "F":  # prev line
            n = nums[0] if nums else 1
            self._set_cursor(r - n, 0)
        elif final == "G":  # column absolute
            n = nums[0] if nums else 1
            self._set_cursor(r, n - 1)
        elif final in ("H", "f"):  # cursor position (1-based)
            rr = nums[0] if nums and nums[0] > 0 else 1
            cc = nums[1] if len(nums) > 1 and nums[1] > 0 else 1
            self._set_cursor(rr - 1, cc - 1)
        elif final == "d":  # VPA line absolute
            n = nums[0] if nums else 1
            self._set_cursor(n - 1, c)
        elif final == "J":  # erase display
            mode = nums[0] if nums else 0
            self._erase_display(mode)
        elif final == "K":  # erase line
            mode = nums[0] if nums else 0
            self._erase_line(mode)
        elif final == "m":  # SGR
            if not private:
                self._sgr_attrs = body
        elif final == "r":  # DECSTBM set scroll region (1-based)
            top = (nums[0] if nums and nums[0] > 0 else 1) - 1
            bottom = (
                nums[1] if len(nums) > 1 and nums[1] > 0 else self.rows
            ) - 1
            if 0 <= top < bottom < self.rows:
                buf.scroll_top = top
                buf.scroll_bottom = bottom
                self._set_cursor(0, 0)
        elif final == "s":
            buf.saved_cursor = (r, c)
        elif final == "u":
            if buf.saved_cursor:
                self._set_cursor(*buf.saved_cursor)
        elif final == "h" and private and "1049" in body:
            self._enter_alt()
        elif final == "l" and private and "1049" in body:
            self._leave_alt()
        elif final == "t":
            # window ops — resize, title 등. (ex. ESC [8;H;Wt)
            if len(nums) >= 3 and nums[0] == 8:
                new_rows, new_cols = nums[1], nums[2]
                if new_rows > 0 and new_cols > 0:
                    self._resize(new_rows, new_cols)
        # 나머지 CSI 는 무시 (DECSET, cursor visibility 등)

    def _erase_display(self, mode: int) -> None:
        buf = self.buf
        r, c = buf.cursor_row, buf.cursor_col
        if mode == 0:
            # cursor to end
            for col in range(c, self.cols):
                buf.grid[r][col] = Cell()
            self.dirty_rows.add(r)
            for row in range(r + 1, self.rows):
                buf.grid[row] = [Cell() for _ in range(self.cols)]
                self.dirty_rows.add(row)
        elif mode == 1:
            for col in range(0, c + 1):
                buf.grid[r][col] = Cell()
            self.dirty_rows.add(r)
            for row in range(0, r):
                buf.grid[row] = [Cell() for _ in range(self.cols)]
                self.dirty_rows.add(row)
        else:  # 2 or 3 — full
            for row in range(self.rows):
                buf.grid[row] = [Cell() for _ in range(self.cols)]
                self.dirty_rows.add(row)

    def _erase_line(self, mode: int) -> None:
        buf = self.buf
        r, c = buf.cursor_row, buf.cursor_col
        if mode == 0:
            for col in range(c, self.cols):
                buf.grid[r][col] = Cell()
        elif mode == 1:
            for col in range(0, c + 1):
                buf.grid[r][col] = Cell()
        else:  # 2
            for col in range(0, self.cols):
                buf.grid[r][col] = Cell()
        self.dirty_rows.add(r)

    def _enter_alt(self) -> None:
        if self._in_alt:
            return
        self._alt = _ScreenBuffer(self.rows, self.cols)
        self._in_alt = True
        self.dirty_rows.update(range(self.rows))

    def _leave_alt(self) -> None:
        if not self._in_alt:
            return
        self._alt = None
        self._in_alt = False
        self.dirty_rows.update(range(self.rows))

    def _resize(self, rows: int, cols: int) -> None:
        # simple strategy: new blank grid, preserve cursor clamped
        self.rows = rows
        self.cols = cols
        old_primary = self._primary
        self._primary = _ScreenBuffer(rows, cols)
        self._primary.cursor_row = min(old_primary.cursor_row, rows - 1)
        self._primary.cursor_col = min(old_primary.cursor_col, cols - 1)
        # copy as much as fits
        for r in range(min(rows, len(old_primary.grid))):
            for c in range(min(cols, len(old_primary.grid[r]))):
                self._primary.grid[r][c] = old_primary.grid[r][c]
        if self._alt is not None:
            self._alt = _ScreenBuffer(rows, cols)
        self.dirty_rows.update(range(rows))


def _parse_params(body: str) -> list[int]:
    """CSI 파라미터 문자열 → int 리스트. 빈 값은 0 으로."""
    if not body:
        return []
    out: list[int] = []
    for part in body.split(";"):
        if not part:
            out.append(0)
            continue
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return out
