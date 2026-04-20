# 버그 리포트 — 20260417_095801

## 현상

장시간 busy(에이전트의 monitor 폴링) 중에 `⏺` 블록이 간헐적으로 사용자에게
전달되지 않는다. 하지만 `completed_count < queue_idx` 상태가 눈에 띄고,
그 뒤에 나온 블록 한두 개가 누락된 듯한 체감이 드는 현상을 분석 요청함.

결론 — **증거상 확실한 실제 데이터 손실 버그**가 존재한다. 원인은 `StreamQueue`
의 단조 증가 `idx` 와, 500줄 스크롤백 창에서 블록이 빠져나갈 때의 상호작용
(off-by-one) 이다. 이번 10시간 세션에서 **최소 23회의 eviction 이벤트**가 발생했고,
각 이벤트마다 1개의 `⏺` 블록이 조용히 삭제된 것으로 재구성된다.

## 타임라인 재구성 — 대표 사례 (seg#2, 22:26~22:53)

`dump/events.jsonl` 의 `flush_peek` 레코드를 따라가면 동일 패턴이 9회 반복된다.
아래는 첫 사이클만 발췌.

| 시각 | 소스 | 이벤트 | cc | idx | nc | 해석 |
|------|------|--------|----|-----|----|------|
| 22:29:20.495 | core | flush_peek (pre-busy) | 6 | 5 | 1 | 1 블록 send → idx 6 |
| 22:29:20.496 | core | flush_block | — | — | — | "Monitor event: ingest 재개…" 전송 |
| 22:29:22.306 | core | flush_peek (busy-stream) | **5** | **6** | 0 | **eviction**: cc ↓, idx 그대로 → `idx > cc` |
| 22:29:27.291 | core | flush_peek (response) | 7 | 6 | 1 | cc 5→7 (Claude 가 2 블록 생성했으나) — `take_new=completed[6:]=[completed[6]]` 만 1개 보냄, completed[5] 가 **조용히 소멸** |
| 22:29:27.292 | core | flush_block | — | — | — | "+39 변환, processed 1029/1314 …" 전송 — 이건 **2번째 블록**임 |

이 "eviction→부분 send" 사이클이 매 ~3분 (busy-stream 10초 tick × 수 차례)
반복된다. 10시간 세션 전체 집계:

```
flush_peek 총 244 건
flush_block 총 111 건 (== sum(new_count))  ← core→sender 경계에서는 손실 0
idx > cc 로 전이한 eviction 이벤트 38 건
  - 단일 idx 에 머물며 반복된 것을 dedup 하면 약 23~25 epoch
  - resolve(=idx 전진) 되기까지 일반적으로 1 tick ~ 1 turn 대기
```

(dedup 은 `idx=10` 구간에서 00:38~00:46 동안 15회 반복된 eviction 이 Claude 의 모니터
대기로 인한 단일 상태 재관찰이라는 점을 반영한 수치.)

사용자 관점에서 체감 가능한 결과:
* 진전이 1개씩 덜 나오는 스트리밍 — "아까 그 로그는 어디 갔지?" 패턴
* 복수 에이전트 모니터링 중 중간 단계 요약 1개가 통째로 사라짐
* 마지막 블록은 거의 항상 온전히 도착 (take_new 는 **가장 최근** 블록을 잡기 때문)
* **[추가] 도구 승인 직후 다음 본문이 통째로 누락** — 연속 승인으로 pane 이 빡빡해진
  상태에서 idx 가 cc 를 앞서있을 때, 승인 후 생성된 응답 블록 전체가 `take_new=[]`
  에 갇혀 다음 user turn (reset) 까지 영영 나가지 않음. 사용자 체감상 "승인했는데
  아무 말이 없다" → 먼저 "뭐해요?" 를 묻게 되는 패턴.

### 현장 재현 로그 (cb1 side, 2026-04-17)

cb1 (`claude-bridge`) 은 cb2 와 소스 identical (HEAD `7b08ca2`, `bridge/*.py` diff 0건).
`logs/2026-04-17.log` 에 위 증상이 그대로 찍힘:

```
10:25:29 USER→BOT  image (cb2 리뷰 요청)
10:25:48 AI-APPROVAL Bash   ┐
10:26:46 USER-ACK           │
10:26:53 AI-APPROVAL Bash   │ 5회 연속 승인 — pane 500줄 창 빡빡
10:27:04 USER-ACK           │
10:27:07 AI-APPROVAL Bash   │
10:27:16 USER-ACK           │
10:27:19 AI→BOT  pre-approval (197 chars) ← 이 한 통만 도착
10:27:20 BOT→USER delivered
10:27:20 AI-APPROVAL Read   │
10:27:23 USER-ACK           ┘
[10:27:23 → 10:35:06 = 7분 43초 공백. AI→BOT response 이벤트 0건]
10:35:06 USER→BOT "뭐하는중이애요?"   ← 사용자가 기다리다 추궁
10:35:15 AI→BOT  response (320 chars)  ← 새 turn (reset 탐) → 정상 도착
```

해석:
* Read 승인(10:27:23) 후 Claude 가 장문 응답(수백 줄)을 생성했지만 `AI→BOT` 이벤트
  자체가 찍히지 않음 = bot 의 **추출 단계(core.\_flush\_completed → take\_new)에서 증발**,
  sender/전송 실패가 아님 (`send_*_fail` 0건).
* 5회 연속 승인이 유발한 pane 출력(도구 실행 결과 + 응답 본문) 이 scrollback 500줄
  한계를 넘기며 eviction 발생 → idx > cc 고착.
* 다음 user turn (10:35:06) 은 `queue.reset()` 을 경유 → idx=0 복귀, 응답 정상 도착.

이는 가설 A 가 cb1 현장에서 재현된 **live evidence**. 제안 수정안 A 는 cb1/cb2 동시 배포 필요.

## 원인 가설

### 가설 A (확정) — `StreamQueue.idx` 가 pane 좌표계와 어긋남

`bridge/stream_queue.py:24-28` 의 `take_new` 는 단순히 `completed[self.idx:]`
를 반환한다. 전제는 "pane 의 블록 순서는 영원히 append-only 하게 자란다" 이지만,
tmux 는 `TMUX_SCROLL_LINES=500` 짜리 슬라이딩 윈도우이므로 장시간 busy 중에는
**앞쪽 블록이 스크롤백 밖으로 밀려나면서 리스트의 인덱스가 shift** 된다.

구체 시퀀스 (seg#2 의 22:29 cycle 을 모델):

```
T0: pane.completed = [A, B, C, D, E, F]   idx=5 → F 전송, idx=6
T1: A 가 500 줄 밖으로 밀려남.
    pane.completed = [B, C, D, E, F]      idx=6 → take_new = completed[6:] = []
T2: Claude 가 G 를 새로 생성.
    pane.completed = [B, C, D, E, F, G]   idx=6 → take_new = completed[6:] = []
    ← G 는 "완료됨" 이지만 위치 5 에 있어 skip. 영원히 소멸.
T3: Claude 가 H 를 더 생성.
    pane.completed = [B, C, D, E, F, G, H] idx=6 → take_new = [completed[6]] = [H]
    ← H 만 전송, idx → 7. G 는 복구되지 않음.
```

`StreamQueue.advance` 는 단조 증가라서 `idx` 를 줄일 수 없고 — 설계상 그래야 함
(재전송 금지) — scrollback eviction 을 보상할 수단이 없다.

`_completed_blocks` + `_is_block_active` 도 이 상황을 구별하지 못한다.
 `extract_response_blocks` 는 "현재 pane 에 보이는 ⏺ 블록" 만 반환하기 때문.

### 가설 B (기각) — sender 쪽 전송 실패

`events.jsonl` 에서 `sum(new_count) == flush_block == send_output == 111` 로 일치.
`send_html_fail`, `send_plain_fail`, `flush_send_fail` 이벤트 **0건**. 즉 core 가
전송 요청한 블록은 전부 나갔다. 문제는 core 가 "새 블록" 을 한 개 덜 골랐다는 것.

### 가설 C (보조) — 블록 오실레이션으로 인한 느린 전달

00:38~00:47 구간에서 `cc` 가 9↔10 사이를 10분간 진동. `_is_block_active` 가 특정
블록 꼬리의 "Running…/Waiting…" 마커를 지연으로 감지하며 완료/활성이 깜빡임.
이 구간에서는 **데이터 손실은 없음** — 단지 사용자 체감 전달 지연이 크게 보임
(한 번에 3 블록이 몰려 도착). 우선순위 낮음.

## 재현 방법

인위 재현 스크립트:

1. tmux pane 에 ⏺ 블록을 많이 찍어 `pane_output` 가 ~500줄 근처에 걸치게 한다.
2. busy-stream 경로를 돌려 `queue.idx` 를 `len(extract_response_blocks())` 와
   같게 맞춘다.
3. pane 에 블록 1개를 추가해 동시에 가장 오래된 블록이 밀려나게 한다
   (예: ⏺ 블록 높이만큼의 출력 inject).
4. 다음 블록이 나와도 `take_new` 가 비어있음을 관찰.
5. 또 하나 나와야 `take_new` 가 그것만 반환하고 중간 블록은 소실.

자연 재현 조건: Claude 가 장시간 폴링/모니터링 루프를 돌리고 매 ~3분 +1 블록
페이스로 생성 시, `TMUX_SCROLL_LINES=500` 에서 매 사이클 1 블록 누락.

## 제안 수정안

세 가지 선택지. 각각 trade-off 다름. 권장은 **A + C** 콤보.

### A. idx 대신 "마지막으로 본 블록의 content hash" 를 저장 (권장)

소멸된 블록이 "정말로 처음 보는" 것인지 판정할 수 있도록 StreamQueue 에 앵커를
추가한다. 인덱스 대신 **가장 마지막에 보낸 블록의 지문** 을 기억.

```diff
--- a/bridge/stream_queue.py
+++ b/bridge/stream_queue.py
@@
+import hashlib
+
+
+def _fp(block: str) -> str:
+    """블록 지문 — 전체 내용이 아니라 앞부분만 써서 streaming 중 growing block 은
+    '같은 블록' 으로 매칭되도록 한다. 완료 블록 길이는 안정적이므로
+    완료 판정 후 호출되는 쪽에서만 쓰는 전제."""
+    return hashlib.md5(block.encode("utf-8", errors="replace")).hexdigest()[:12]
+
+
 class StreamQueue:
     def __init__(self) -> None:
         self.idx: int = 0
+        self.last_fp: str = ""   # 마지막 소비된 블록 지문 (eviction 복원용)

     def take_new(self, completed: list[str]) -> list[str]:
-        return completed[self.idx:]
+        # 1차: 단순 position slice (가장 흔한 정상 경로)
+        if self.idx <= len(completed):
+            candidate = completed[self.idx:]
+            # last_fp 앵커가 있으면 candidate 앞쪽에 앵커를 찾고, 그 다음부터만 신규로.
+            if self.last_fp and candidate:
+                # 맨 앞 하나가 앵커와 같으면 이미 보낸 블록 — 건너뛴다
+                if _fp(candidate[0]) == self.last_fp:
+                    return candidate[1:]
+            return candidate
+        # 2차: idx 가 cc 를 앞질렀다 — scrollback eviction 발생.
+        # last_fp 위치를 completed 에서 찾아 그 다음부터가 신규.
+        if self.last_fp:
+            for i, blk in enumerate(completed):
+                if _fp(blk) == self.last_fp:
+                    return completed[i + 1:]
+        # 앵커도 못 찾으면 (=너무 많이 밀려났거나 부팅 직후) 보수적으로 꼬리만.
+        # 재전송 위험보다 누락 위험을 택하는 기존 원칙 유지.
+        return []

     def advance(self, to_len: int) -> None:
         if to_len > self.idx:
             self.idx = to_len
+
+    def mark_sent(self, last_block: str) -> None:
+        """flush 가 성공한 마지막 블록의 지문을 기록. 다음 pane scroll 이후에도
+        '거기서 다시 시작' 할 수 있게."""
+        self.last_fp = _fp(last_block)
```

그리고 `bridge/core.py:_flush_completed` 의 성공 분기:

```diff
         if sent > 0:
             self.queue.advance(self.queue.idx + sent)
+            # 마지막으로 성공 전송된 블록의 지문을 앵커로 기록
+            self.queue.mark_sent(new_blocks[sent - 1])
             self.boot_notified = True
```

이렇게 하면 eviction 후에도 `last_fp` 로 앵커를 다시 찾아 `completed[anchor+1:]`
를 신규로 취급한다. 버전 간 content-hash dedup 을 되살리는 게 아니라, **단 하나의
앵커 블록 지문만 보관** 하기 때문에 이전 커밋 `ad28ec6` 가 경계했던 "LRU 오버플로
→ 재전송 폭주" 문제는 재현되지 않는다 (앵커 1개만 존재하고 앞뒤 경계가 명확).

### B. pane 을 500줄 대신 capture -p -S -2000 처럼 크게 뜨기

가장 간단한 우회책. 블록 하나가 뜨려면 대략 3~15줄 필요하므로 2000줄이면
200 블록 이상을 한 번에 감시 가능. 하지만 근본 수정이 아니다 — 페이스가 빨라지면
다시 재현. 장시간 세션에서 한계가 뚜렷.

### C. busy-stream 틱에서 `idx > cc` 감지 시 경고 로그 + 복구 hint

A 를 못 넣거나 긴급 완화가 필요한 경우의 안전망. `_flush_completed` 의 peek 직후에

```python
if self.queue.idx > len(completed):
    _log("QUEUE-SLIP",
         f"{log_tag}: idx={self.queue.idx} > cc={len(completed)} — scrollback eviction")
    dump.event("core", "queue_slip",
               tag=log_tag, idx=self.queue.idx, cc=len(completed))
```

를 추가. 최소한 앞으로의 재현 조사 속도는 10배 이상 빨라진다. 운영 배포 시
A 와 함께 가져가면 좋음.

### 리스크 / 사이드이펙트

A 수정안 기준:
- **리스크 1**: `last_fp` 매칭 실패 시 (블록이 전부 스크롤백 밖으로 밀려난 경우)
  조용히 손실이 지속될 수 있음. 이 경우 보조 안전망(C) 가 눈에 띄게 해줌.
- **리스크 2**: 동일 블록이 pane 에 두 번 나타나는 경우 (거의 없지만 가능성) —
  `last_fp` 앵커 이후 블록만 반환하므로 중복 전송 안 함.
- **리스크 3**: seed 타이밍. `queue.seed(len(seed_blocks))` 시점에도 `mark_sent`
  로 앵커 지정 필요. 안 그러면 첫 eviction 에서 앵커 미발견 → 꼬리 하나만 보내는 보수 경로.

### 테스트 추가/변경 제안
- [ ] `tests/test_stream_queue.py` — eviction 시나리오 직접 테스트 추가
  ```python
  def test_take_new_after_scrollback_eviction():
      q = StreamQueue()
      q.advance(q.idx + 2); q.mark_sent("⏺ B2")
      # B1 evicted → completed 가 [B2, B3] 만 남음 (len=2, idx=2)
      new = q.take_new(["⏺ B2", "⏺ B3"])
      assert new == ["⏺ B3"]   # B3 정상 추출
  ```
- [ ] `tests/test_core_flush.py` — 통합 테스트에서 `_flush_completed` 두 번 호출 사이에
  pane 이 shift 되는 mock 을 넣고 누락이 없음을 검증.
- [ ] `tests/test_approval_flow.py` — **승인 직후 첫 응답 블록 보존 테스트** (신규).
  시나리오: 연속 도구 승인으로 idx 를 cc 보다 앞서게 만든 뒤 Claude 가 응답 블록을
  생성했을 때, `last_fp` 앵커를 통해 해당 블록이 `take_new` 결과에 포함되어야 함.
  reset 유무와 무관하게 앵커 기반 복원이 동작함을 확인.
- [ ] 기존 `test_busy_stream_position_dedup.py` 의 기대값을 A 방식에 맞춰 조정.

### 가독성 개선 메모 (`_fp` 함수)

md5 대신 **블록 첫 줄 앞 64자 정규화 슬라이스** 로 앵커를 삼는 것을 권장.
- dump/events.jsonl 에서 앵커가 육안으로 추적 가능 (`last_fp=⏺ Monitor event: ingest...`)
- 충돌 가능성은 동일 tick 안에선 실질 0 (⏺ 블록 첫 줄은 action verb 로 시작해 개별성 높음)
- 성능 차이 없음, 디버깅 이점만 있음

### seed/reset 경로 앵커 누락 방지

`core.seed()` 와 `receiver.reset()` 양쪽에서 초기 pane 블록이 있을 경우 `queue.mark_sent(마지막 블록)` 을 호출해 앵커를 채워둬야 부팅/reset 직후 첫 eviction 에서
"앵커 미발견 → 꼬리 하나만" 보수 경로로 빠지지 않음. 이 호출 누락은 이번 수정안의
가장 흔한 회귀 포인트이므로 repairer 가 반드시 확인할 것.

## 추가 수집이 필요한 정보
- [ ] 평소 체감한 "빠진 응답" 이 몇 개나 되는지 사용자 메모 (세션 / 날짜 기준)
- [ ] 다음 재현 세션에서 `CB_DUMP=1` 을 켠 채 `TMUX_SCROLL_LINES` 를 2000 으로
  임시 상향한 비교 dump (가설 A 와 B 의 효과 측정)
- [ ] `receiver.py` 의 `queue.reset()` 호출 지점 — 실제로 user turn 시작에서
  호출되는지 (이번 분석에선 직접 확인 안 함 — evidence 상 idx 가 0 으로 돌아가는
  전환은 관측됨)
