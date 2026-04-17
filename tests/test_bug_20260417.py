"""
20260417_095801 회귀 방지 테스트 — scrollback eviction 시 `⏺` 블록 누락.

문제: `StreamQueue` 가 pane 에 있던 앞쪽 블록이 tmux scrollback (500줄) 밖으로
밀려날 때 `idx` 가 pane 좌표계를 앞지르며 `completed[idx:]` 가 빈 리스트가 되거나
`idx > len(completed)` 상태로 고착 — 조용한 데이터 누락.

해결: `last_fp` 앵커를 도입해 eviction 을 감지하고 (`detect_slip`) 앵커 위치 기준
으로 새 블록을 슬라이스. `advance_past` 로 전송 후 idx 를 pane 좌표계에 재정렬.
"""
from __future__ import annotations


def _block(sig: str, body: str = "detail") -> str:
    """테스트용 완료 ⏺ 블록 ('Running…/Waiting…' 없이 stable)."""
    return f"⏺ {sig}\n  ⎿ {body}"


# ---------- detect_slip ----------

def test_detect_slip_none_on_empty_queue():
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    assert q.detect_slip([_block("A"), _block("B")]) is None


def test_detect_slip_none_when_anchor_matches_prior():
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    A, B = _block("A"), _block("B")
    q.advance(2)
    q.mark_sent(B)
    assert q.detect_slip([A, B]) is None


def test_detect_slip_idx_past_cc():
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    q.advance(5)
    q.mark_sent(_block("E"))
    assert q.detect_slip([_block("C"), _block("D")]) == "idx_past_cc"


def test_detect_slip_anchor_mismatch_when_prior_shifted():
    """T2 시나리오: idx == cc 지만 앵커가 마지막 위치와 불일치 → eviction."""
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    A, B, C, D, E, F = (_block(x) for x in "ABCDEF")
    q.advance(6)
    q.mark_sent(F)
    # A evicted, G 추가: len=6, idx=6 이지만 completed[5]=G != anchor F
    G = _block("G")
    completed = [B, C, D, E, F, G]
    assert q.detect_slip(completed) == "anchor_mismatch"


# ---------- take_new under eviction ----------

def test_take_new_after_single_eviction_recovers_lost_block():
    """BUG_REPORT T2: G 가 pane 에 있지만 idx==cc 로 누락되는 현상이 복원되어야."""
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    A, B, C, D, E, F = (_block(x) for x in "ABCDEF")
    q.advance(6)
    q.mark_sent(F)
    G = _block("G")
    # A evicted, G 추가
    completed = [B, C, D, E, F, G]
    assert q.take_new(completed) == [G]


def test_take_new_after_eviction_with_idx_past_cc():
    """idx > cc 순간(새 블록이 아직 안 붙은 상태) → 보낼 게 없음. 앵커 기반 재탐색."""
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    A, B, C, D, E, F = (_block(x) for x in "ABCDEF")
    q.advance(6)
    q.mark_sent(F)
    # A evicted, 아직 G 없음: idx=6 > cc=5
    completed = [B, C, D, E, F]
    assert q.take_new(completed) == []


def test_take_new_bug_report_test_case():
    """BUG_REPORT 에 제시된 정확한 테스트 케이스.

    B1 이 evicted 되고 B2 는 이미 보낸 상태, B3 이 새로 추가된 상황에서
    B3 만 정상적으로 추출되어야 한다.
    """
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    B2 = _block("B2")
    B3 = _block("B3")
    q.advance(2)
    q.mark_sent(B2)
    # B1 evicted → completed = [B2, B3], len=2, idx=2
    new = q.take_new([B2, B3])
    assert new == [B3]


def test_take_new_multi_block_recovery_across_eviction():
    """BUG_REPORT T3: G 가 누락 없이 H 와 함께 전달되어야 한다."""
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    A, B, C, D, E, F = (_block(x) for x in "ABCDEF")
    G, H = _block("G"), _block("H")
    q.advance(6)
    q.mark_sent(F)
    completed = [B, C, D, E, F, G, H]  # A evicted, G+H 추가
    assert q.take_new(completed) == [G, H]


def test_take_new_anchor_lost_returns_empty_conservatively():
    """앵커가 완전히 scrollback 밖으로 밀려나 사라졌을 때 재전송 폭주 방지.

    재전송 위험보다 누락 위험을 택하는 기존 원칙 유지.
    """
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    q.advance(10)
    q.mark_sent(_block("X_far_away"))
    # completed 에 앵커가 없음 — pane 이 너무 많이 밀려난 상황
    completed = [_block("M"), _block("N"), _block("O")]
    assert q.take_new(completed) == []


# ---------- advance_past ----------

def test_advance_past_realigns_idx_to_block_position():
    """전송 후 idx 는 pane 에서 마지막 보낸 블록 다음 위치로 재정렬된다."""
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    A, B, C = _block("A"), _block("B"), _block("C")
    q.advance_past([A, B, C], B)
    assert q.idx == 2


def test_advance_past_after_eviction_uses_shifted_position():
    """eviction 으로 앵커가 shift 된 경우에도 새 좌표에 맞게 정렬."""
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    B, C, D, E, F = (_block(x) for x in "BCDEF")
    G = _block("G")
    q.advance(6)  # 이전 좌표계에서 F 가 position 5 였을 때
    q.mark_sent(F)
    # A evicted, G 추가 — F 가 position 4 로 내려옴
    completed = [B, C, D, E, F, G]
    q.advance_past(completed, G)
    assert q.idx == 6  # G 가 position 5, idx=6 (F 때 idx 와 같지만 좌표계가 다름)


def test_advance_past_missing_block_falls_back_to_monotone():
    """아주 드문 경우: 전송한 블록이 완료 리스트에서 사라짐 → idx+1 fallback."""
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    q.advance(5)
    q.advance_past([_block("X"), _block("Y")], _block("gone"))
    assert q.idx == 6


# ---------- reset clears anchor ----------

def test_reset_clears_anchor():
    """reset 이 idx 뿐 아니라 last_fp 도 초기화해야 한다."""
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    q.advance(3)
    q.mark_sent(_block("B"))
    assert q.last_fp
    q.reset()
    assert q.idx == 0
    assert q.last_fp == ""


# ---------- 승인 흐름 — BUG_REPORT 의 두 번째 증상 ----------

def test_approval_does_not_lose_first_response_block():
    """도구 승인 직후 첫 응답 블록이 eviction 으로 누락되는 현상이 복원됨.

    시나리오:
    1. 연속 도구 승인으로 pane 이 빡빡해짐
    2. 승인 중 pre-approval flush 가 마지막으로 블록 N 을 보냄 (idx=N, anchor=블록 N)
    3. 승인 처리 + 도구 실행 중 앞 블록이 scrollback 밖으로 밀려나고 응답 블록들이 새로 붙음
    4. 다음 flush 에서 `anchor_mismatch` 가 감지되어 앵커 기반으로 응답 블록들이 정상 추출
    """
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    A, B, C, D, E, F = (_block(x) for x in "ABCDEF")
    # pre-approval flush 후 상태
    q.advance(6)
    q.mark_sent(F)
    # 승인 처리 중 A 가 밀려나고 Claude 가 응답 블록 2개 생성
    R1 = _block("Response1", "first response body")
    R2 = _block("Response2", "second response body")
    completed = [B, C, D, E, F, R1, R2]  # A evicted, R1 R2 추가
    new = q.take_new(completed)
    assert new == [R1, R2], "승인 직후 응답 블록이 모두 보존되어야 함"


def test_reset_then_next_turn_works_without_stale_anchor():
    """새 user turn (reset) 이후 앵커가 깨끗이 초기화되어 다음 flush 에서
    오작동하지 않음을 확인."""
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    q.advance(5)
    q.mark_sent(_block("old_turn_last"))
    q.reset()
    # 새 turn: pane 에 이전 블록 + 새 블록 섞임 (reset 시맨틱은 '다 새로 본다')
    completed = [_block("old"), _block("new_turn_1"), _block("new_turn_2")]
    # 앵커 없음 + idx=0 → 정상 경로
    assert q.take_new(completed) == completed


if __name__ == "__main__":
    import sys
    import traceback
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if failures else 0)
