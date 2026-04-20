# case-03 — Decision Gate 평가 (raw+VT 전환 실효성)

## 현재 아키텍처의 실패 메커니즘 (압축)

`capture-pane -S -500` 은 **최근 500줄만** 반환. 10시간 세션에서 오래된 `⏺` 블록은
창 밖으로 밀려 `lines[]` 에서 사라짐. StreamQueue 의 `idx`/`last_fp` 는 "창 안에서
몇 번째 블록" 가정하에 동작하는데, 앵커가 창 밖으로 나가면 `detect_slip` 가
false-negative → `take_new` 가 `[]` 반환 (**로그도 남지 않는 침묵 경로**).
BUG_REPORT 추정 23회 eviction × 1 블록/건 = **23건 데이터 소실**.

## raw+VT 전환 시

### Category-A (raw 가 **유일하게** 해결): **최강 (존재 이유)**

snapshot 창 (`-S -500`) 이라는 개념 자체가 없어짐. pipe-pane 은 **append-only
file**, 분석기 커서는 **byte offset**. 다음이 구조적으로 보장:

- eviction 불가능 — file 은 append 만 되고 분석기가 과거로 돌아갈 일 없음.
- StreamQueue 의 `idx`/`last_fp`/`detect_slip`/`advance_past` 전부 **기계적 제거 가능**
  — 분석기가 발화한 이벤트를 "이미 sent" 로 marking 하는 단순 offset-dedup 으로 치환.
- 500 이라는 magic number 제거. 10시간 세션이든 1주일 세션이든 동작.

현재 아키텍처에서는 `TMUX_SCROLL_LINES` 를 5000 으로 늘려도 **시간을 벌 뿐** —
원리적 한계 그대로. 이 케이스가 "**snapshot 대 stream 의 패러다임 격차**" 를 가장
명확히 증명하는 증거.

### Category-B (부수적 아키텍처 청소): **강**

- StreamQueue 모듈 전체 제거 (`stream_queue.py` 의 복잡한 slip 감지 로직 소멸).
- 관련 테스트 (`tests/test_stream_queue.py`) 도 대거 단순화.
- `take_new()` 의 "침묵 경로" 문제 소멸 — offset 기반 dedup 은 "이미 fire 된 offset"
  를 다시 fire 하려고 하면 즉시 noop 이고 로깅 가능.

### Category-C (미래 내성): **강**

사용자 세션이 수일/수주 지속돼도 분석 품질 불변. `tmux show-options scrollback-limit`
변경에도 의존하지 않음.

## Decision Gate 기여도

**최강 — keystone 케이스**. 이 케이스 하나로 "왜 snapshot 으로 남아있을 수 없나"
의 답이 나옴. case-01/02 가 알고리즘 개선 여지가 있는 것과 달리, case-03 은
**창 구조 자체를 버려야** 해결.

"raw+VT 전환 가치" 논거의 **근거 블록**. 다른 케이스들이 설득력 없어도 이 케이스
하나로 Step 3 게이트 통과 근거 성립.

## Step 2 분석기 요구사항 (이 케이스에서 추출)

1. **file offset 기반 cursor**: `(fd, pos_bytes)` 가 분석기 상태. 커서는 단조 증가.
2. **rotation 통합**: pipe-pane log 가 20MB rotation 되면 분석기 커서도 새 파일로
   넘어가되, rotate 경계에서 이벤트 연속성 유지 (rotate 시점 flush 완료 후 신파일).
3. **이벤트 dedup**: 한 번 발화된 `(offset, event_type)` 튜플은 재발화 안 됨 —
   state 가 아니라 append-only event log 에 남김.
4. **메모리 상한**: 분석기는 raw log 전체를 메모리에 올리지 않음. sliding window
   로 충분 (event 발화 후 더 뒤쪽은 잊어도 됨).
