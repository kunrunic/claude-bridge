"""Position-based + anchor-backed ⏺ 블록 전달 큐.

커밋 ad28ec6 의 position idx 설계에 **last_fp 앵커** 를 추가해 tmux scrollback
(TMUX_SCROLL_LINES=500) 에서 앞 블록이 밀려날 때 발생하는 off-by-one 누락
(bugreport 20260417_095801 참조) 을 복구한다.

    pane → 블록 추출 → take_new (정상 slice / eviction 시 앵커 재탐색)
         → 전송 → advance_past + mark_sent → 다음 tick

핵심 원칙
---------
* 보낸 블록은 "소비됨" — 동일 문자열이 다시 등장해도 재전송하지 않는다.
* idx 는 단조증가. scrollback eviction 시에는 앵커로 재탐색해 pane 좌표계로
  재정렬하지만, 결과적 idx 값은 이전보다 크거나 같다.
* 앵커 유실 시에는 보수적으로 빈 리스트 반환 (재전송 폭주보다 누락이 낫다).
* 큐에 실제 데이터를 쌓지 않음. pane 이 원천 source, idx/앵커만 상태로 보유.
"""
from __future__ import annotations


def _fp(block: str) -> str:
    """블록 앵커 지문. 완료 블록 첫 줄의 앞 64자 (strip).

    md5 대신 육안 추적 가능한 형태 — dump/events.jsonl 에서 `last_fp=⏺ Bash(...)`
    로 바로 보임. 완료된 ⏺ 블록 첫 줄은 action signature 로 시작해 충돌 위험 실질 0.
    `_is_block_active` 로 필터된 완료 블록만 대상이므로 내용이 더 이상 자라지 않음.
    """
    if not block:
        return ""
    first = block.splitlines()[0] if "\n" in block else block
    return first.strip()[:64]


class StreamQueue:
    def __init__(self) -> None:
        self.idx: int = 0
        self.last_fp: str = ""   # 마지막 소비된 블록 앵커 (eviction 복원용)

    def detect_slip(self, completed: list[str]) -> str | None:
        """scrollback eviction 감지. 반환값은 slip 종류 — 없으면 None.

        - "idx_past_cc": idx 가 현재 블록 수를 이미 앞서감 (강한 신호)
        - "anchor_mismatch": idx 위치 바로 앞이 우리가 마지막에 보낸 블록이 아님
          (앞쪽 블록이 밀려나 idx 와 pane 좌표가 어긋남)
        """
        if self.idx > len(completed):
            return "idx_past_cc"
        if self.last_fp and 0 < self.idx <= len(completed):
            prior = completed[self.idx - 1]
            if _fp(prior) != self.last_fp:
                return "anchor_mismatch"
        return None

    def take_new(self, completed: list[str]) -> list[str]:
        """아직 소비되지 않은 블록만 반환. 호출 자체로는 상태를 변경하지 않는다.
        호출자는 전송 성공 후 `advance_past` + `mark_sent` 를 호출해야 한다.

        경로:
          1. slip 없음 → completed[idx:] (정상 경로)
          2. slip 감지 + 앵커 보유 → 앵커 위치 찾아 completed[anchor+1:]
          3. slip 감지 + 앵커 유실 → [] (보수적; 재전송 폭주 방지)
        """
        slip = self.detect_slip(completed)
        if slip is None:
            return completed[self.idx:]
        if self.last_fp:
            for i, blk in enumerate(completed):
                if _fp(blk) == self.last_fp:
                    return completed[i + 1:]
        return []

    def advance(self, to_len: int) -> None:
        """idx 를 `to_len` 으로 전진. 단조증가 — 뒤로는 가지 않는다."""
        if to_len > self.idx:
            self.idx = to_len

    def advance_past(self, completed: list[str], last_block: str) -> None:
        """pane 좌표계에서 `last_block` 다음 위치로 idx 를 전진.
        eviction 으로 이동한 경우에도 앵커 재탐색해 정확히 정렬.
        완전히 유실되면 기존 단조증가 advance(idx+1) 로 fallback.
        """
        for i in range(len(completed) - 1, -1, -1):
            if completed[i] == last_block:
                self.advance(i + 1)
                return
        self.advance(self.idx + 1)

    def mark_sent(self, last_block: str) -> None:
        """flush 에 성공한 마지막 블록의 앵커를 기록. 다음 eviction 이후에도
        '여기서 다시 시작' 할 수 있게. seed/reset 직후에도 호출해 둬야
        첫 eviction 에서 앵커 미발견 → 보수 경로로 빠지지 않는다.
        """
        self.last_fp = _fp(last_block)

    def seed(self, to_len: int) -> None:
        """부팅 시 seed: pane 에 이미 있던 N 개 블록을 '소비됨' 으로 간주.
        호출자는 직후 `mark_sent(seed_blocks[-1])` 를 함께 호출해 앵커를 채워야 한다.
        """
        self.idx = to_len

    def reset(self) -> None:
        """세션 재시작 / 새 user turn — idx 와 앵커 모두 초기화."""
        self.idx = 0
        self.last_fp = ""
