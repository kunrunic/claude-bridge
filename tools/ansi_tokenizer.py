"""ANSI 스트리밍 토크나이저 — step-2-spec [1].

입력: 바이트 스트림 (session offset + chunk 페어)
출력: Token(offset_start, offset_end, kind, payload) 시퀀스

kinds:
    text          — UTF-8 printable (box-drawing 포함). payload=str
    csi           — CSI sequence (ESC [ ... final). payload={"params": str, "final": str}
    osc           — OSC sequence (ESC ] ... ST|BEL). payload=str
    cr            — \\r
    lf            — \\n
    bs            — \\b
    ht            — \\t
    alt_screen_on — CSI ?1049h 승격 토큰 (step-2-spec 4.2 modal hint)
    alt_screen_off — CSI ?1049l 승격 토큰
    other         — 분류 불가 (rare: sub-escape sequence, partial UTF-8 fragment)

설계 노트:
- **stateful**: chunk 경계에 걸친 CSI/OSC/UTF-8 시퀀스를 처리하려면 pending buffer
  필요. `Tokenizer` 클래스가 self.pending 에 유지.
- **scanner loop 재개 지점**: 각 iteration 에서 첫 ESC 를 찾아 prefix text 를
  분리, 그 다음 ESC 시퀀스를 완결 가능한지 검사, 불가 시 pending 에 남김.
- **offset 의미**: Token 의 offset_start/end 는 session 누적 byte. UTF-8 디코드
  전 byte 기준 — Token.payload 가 "abc" (3 chars) 여도 UTF-8 멀티바이트면
  offset 차이 ≠ len(text).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Literal

TokenKind = Literal[
    "text",
    "csi",
    "osc",
    "cr",
    "lf",
    "bs",
    "ht",
    "alt_screen_on",
    "alt_screen_off",
    "other",
]


@dataclass
class Token:
    offset_start: int
    offset_end: int
    kind: TokenKind
    payload: object  # text: str / csi: dict / osc: str / controls: None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"Token({self.offset_start}-{self.offset_end}, {self.kind}, "
            f"{self.payload!r})"
        )


_ESC = 0x1B
_CR = 0x0D
_LF = 0x0A
_BS = 0x08
_HT = 0x09
_BEL = 0x07


class Tokenizer:
    """chunk 단위로 feed 받아 Token 을 yield 하는 streaming 토크나이저."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._buf_offset = 0  # session offset of self._buf[0]
        # §13.4 — incomplete escape sequence 로 인해 buffering 된 횟수.
        # drain 종료 시 pending 이 남아있으면 증가. 운영 중 0 이 유지돼야 정상.
        self.incomplete_escape_count: int = 0
        # final=True 에서 incomplete 를 `other` 로 배출한 횟수 (실제 desync risk).
        self.incomplete_escape_flushed: int = 0

    def feed(self, chunk_offset: int, chunk: bytes) -> Iterator[Token]:
        """chunk 를 추가로 받아 가능한 만큼 tokenize. chunk_offset 은 chunk[0] 의
        session offset. 내부 pending buffer 와 반드시 연속이어야 한다."""

        if not self._buf:
            self._buf_offset = chunk_offset
        else:
            expected = self._buf_offset + len(self._buf)
            if chunk_offset != expected:
                raise ValueError(
                    f"non-contiguous feed: buf_end={expected} got={chunk_offset}"
                )
        self._buf.extend(chunk)
        yield from self._drain(final=False)

    def flush(self) -> Iterator[Token]:
        """EOF 시 호출. pending 의 미완결 시퀀스를 text/other 로 배출."""

        yield from self._drain(final=True)

    def _drain(self, *, final: bool) -> Iterator[Token]:
        i = 0
        buf = self._buf
        n = len(buf)
        base = self._buf_offset

        while i < n:
            b = buf[i]

            # C0 single-byte controls
            if b == _CR:
                yield Token(base + i, base + i + 1, "cr", None)
                i += 1
                continue
            if b == _LF:
                yield Token(base + i, base + i + 1, "lf", None)
                i += 1
                continue
            if b == _BS:
                yield Token(base + i, base + i + 1, "bs", None)
                i += 1
                continue
            if b == _HT:
                yield Token(base + i, base + i + 1, "ht", None)
                i += 1
                continue

            if b == _ESC:
                consumed = self._try_escape(i, final=final)
                if consumed is None:
                    # incomplete escape — pending for next chunk (final 시엔
                    # _try_escape 내부의 _esc_alone 이 이미 other 로 배출).
                    self.incomplete_escape_count += 1
                    break
                tok, next_i = consumed
                if tok is not None:
                    yield tok
                i = next_i
                continue

            # text run — up to next control/ESC
            j = i
            while j < n:
                c = buf[j]
                if c in (_ESC, _CR, _LF, _BS, _HT):
                    break
                j += 1
            raw = bytes(buf[i:j])
            # UTF-8 decode — incomplete trailing bytes stay pending unless final
            try:
                text = raw.decode("utf-8")
                yield Token(base + i, base + j, "text", text)
                i = j
            except UnicodeDecodeError as e:
                good = raw[: e.start]
                if good:
                    yield Token(
                        base + i,
                        base + i + e.start,
                        "text",
                        good.decode("utf-8"),
                    )
                    i += e.start
                # remaining is either partial UTF-8 at end-of-buf, or a bad byte
                # mid-run.
                rest_end = j
                at_eob = (rest_end == n)
                if at_eob and not final:
                    # incomplete UTF-8 — 동일하게 카운트 (next chunk 대기).
                    self.incomplete_escape_count += 1
                    break  # wait for more bytes
                # bad byte or final flush — emit as "other"
                bad_len = max(1, rest_end - i)
                yield Token(
                    base + i,
                    base + i + bad_len,
                    "other",
                    bytes(buf[i : i + bad_len]),
                )
                i += bad_len

        # compact: drop consumed prefix
        if i > 0:
            del buf[:i]
            self._buf_offset += i

    def _try_escape(
        self, i: int, *, final: bool
    ) -> tuple[Token | None, int] | None:
        """ESC at buf[i]. Returns (token, next_i) if a full sequence was consumed,
        or None if we need more bytes (and not final)."""

        buf = self._buf
        n = len(buf)
        base = self._buf_offset

        if i + 1 >= n:
            return None if not final else self._esc_alone(i)

        second = buf[i + 1]

        # CSI: ESC [
        if second == 0x5B:  # '['
            # scan params (0x30..0x3F), intermediates (0x20..0x2F), final (0x40..0x7E)
            j = i + 2
            while j < n and 0x30 <= buf[j] <= 0x3F:
                j += 1
            while j < n and 0x20 <= buf[j] <= 0x2F:
                j += 1
            if j >= n:
                return None if not final else self._esc_alone(i)
            final_byte = buf[j]
            if 0x40 <= final_byte <= 0x7E:
                params = bytes(buf[i + 2 : j]).decode("ascii", errors="replace")
                final_ch = chr(final_byte)
                kind: TokenKind = "csi"
                payload: object = {"params": params, "final": final_ch}
                # alt-screen 승격
                if params == "?1049" and final_ch == "h":
                    kind = "alt_screen_on"
                    payload = None
                elif params == "?1049" and final_ch == "l":
                    kind = "alt_screen_off"
                    payload = None
                tok = Token(base + i, base + j + 1, kind, payload)
                return (tok, j + 1)
            # not a valid CSI — treat as "other" of 2 bytes
            return (
                Token(base + i, base + i + 2, "other", bytes(buf[i : i + 2])),
                i + 2,
            )

        # OSC: ESC ]  ... (BEL | ST=ESC\\)
        if second == 0x5D:  # ']'
            j = i + 2
            while j < n:
                if buf[j] == _BEL:
                    payload = bytes(buf[i + 2 : j]).decode("utf-8", errors="replace")
                    return (
                        Token(base + i, base + j + 1, "osc", payload),
                        j + 1,
                    )
                if buf[j] == _ESC and j + 1 < n and buf[j + 1] == 0x5C:
                    payload = bytes(buf[i + 2 : j]).decode("utf-8", errors="replace")
                    return (
                        Token(base + i, base + j + 2, "osc", payload),
                        j + 2,
                    )
                if buf[j] == _ESC:
                    # could be start of ST but we need the next byte
                    if j + 1 >= n:
                        return None if not final else self._esc_alone(i)
                j += 1
            return None if not final else self._esc_alone(i)

        # ESC + single-char escapes (e.g. ESC 7, ESC c, ESC D). treat as 2-byte
        # "other". RIS (ESC c) / IND (ESC D) could be promoted later if needed.
        return (
            Token(base + i, base + i + 2, "other", bytes(buf[i : i + 2])),
            i + 2,
        )

    def _esc_alone(self, i: int) -> tuple[Token, int]:
        """flush 시 미완결 ESC 시퀀스를 'other' 로 배출."""
        buf = self._buf
        n = len(buf)
        base = self._buf_offset
        self.incomplete_escape_flushed += 1
        return (
            Token(base + i, base + n, "other", bytes(buf[i:n])),
            n,
        )


def tokenize_bytes(data: bytes, *, offset0: int = 0) -> list[Token]:
    """편의 함수: 통째 bytes 입력 → Token 리스트. 테스트용."""
    tok = Tokenizer()
    out: list[Token] = list(tok.feed(offset0, data))
    out.extend(tok.flush())
    return out
