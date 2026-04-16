"""Position-based ⏺ 블록 전달 큐.

`content-hash 비교로 dedup 하던 기존 설계` 를 폐기하고 단순한 인덱스 하나로
producer-consumer 흐름을 표현한다.

    pane → 블록 추출 → 새 블록만 consume → 전송 → idx 전진 → 끝

핵심 원칙
---------
* 보낸 블록은 "소비됨" — 동일 문자열이 다시 등장해도 재전송하지 않는다.
* idx 는 단조증가. 스크롤백으로 앞 블록이 잘려나가도 retreat 하지 않는다
  (재전송보다 유실을 택하는 쪽이 사용자 혼란이 덜함).
* content 비교/TTL/LRU 없음 — 상태는 정수 하나뿐.
* 큐에 실제 데이터를 쌓지 않음. 어차피 pane 이 원천 source 이고 idx 만 있으면
  `blocks[idx:]` 로 그때그때 slice 해서 꺼내는 게 충분하다.
"""
from __future__ import annotations


class StreamQueue:
    def __init__(self) -> None:
        self.idx: int = 0

    def take_new(self, completed: list[str]) -> list[str]:
        """아직 소비되지 않은 블록만 반환. 이 호출 자체로는 idx 를 움직이지 않는다.
        호출자는 실제 전송 성공 후 `advance(len(completed))` 를 호출해야 한다.
        """
        return completed[self.idx:]

    def advance(self, to_len: int) -> None:
        """idx 를 `to_len` 으로 전진. 단조증가 — 뒤로는 가지 않는다."""
        if to_len > self.idx:
            self.idx = to_len

    def seed(self, to_len: int) -> None:
        """부팅 시 seed: pane 에 이미 있던 N 개 블록을 '소비됨' 으로 간주."""
        self.idx = to_len

    def reset(self) -> None:
        """세션 재시작 — idx 0 으로 되돌림."""
        self.idx = 0
