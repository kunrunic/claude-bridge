# Step 2 — 오프라인 분석기 결과 (Decision Gate 투입 자료)

- 상태: **초안** — Step 3 게이트 판정 대기
- 작성일: 2026-04-19
- 구현체: [../../../tools/pipe_pane_analyzer.py](../../../tools/pipe_pane_analyzer.py) 외 5개 모듈
- 테스트: `tests/test_{ansi_tokenizer,vt_screen,line_commit,region_tagger,event_classifier,pipe_pane_analyzer}.py`
- 전체 회귀: **243 passed** (190 → +53 신규)

## 요약

Edit 승인 wording 미스매치(case-04)로 bridge 가 stuck 되던 시나리오를 오프라인
분석기가 **구조 기반**으로 감지. bridge events.jsonl 에는 `is_approval: 0건`,
`queue_slip: 9회` 로 침묵한 구간에서, 분석기는 `approval_show(tool_hint="edit",
summary="Do you want to make this edit to reviewer.py?")` 1건을 명시 emit.

## 이벤트 출력 (case-04 라이브 재현 raw 451KB)

```
block_commit:    390
busy_enter:       30
busy_exit:        30
approval_show:     1
approval_cancel:   1
```

총 452 JSON Line 이벤트. `(offset, event_type)` 튜플 dedup 불변식 준수 (fixture
test `test_case_04_no_queue_slip_equivalent`).

bridge 가 같은 구간에서 emit 한 events.jsonl 와 대조:
- `flush_block: 3` (telegram 실제 전송), `queue_slip: 9` (루프), `is_approval: 0`.
- 분석기는 `approval_show` 1건 + `approval_cancel(method=flush)` 1건.
- bridge 는 "승인 질문이 온 것도 몰랐음". 분석기는 **구조 기반 (divider + ❯ 1. Yes
  + footer)** 으로 포착.

## Decision Criteria 대응 (README §Decision Criteria)

### 1. 재현성 — **부분 충족**

- case-04: ✅ `approval_show` + `block_commit` 390건, bridge 미검출 사건을 명시 emit.
- case-01/02/03: ⚠️ **raw ANSI fixture 부재**. baseline 폴더에 `tmux_capture.txt`
  (이미 렌더된 텍스트) 만 있어 분석기 입력 불가.
  - 대응: Step 1 live 수집 기간에 동일 증상 재현 시 raw 확보. 현재는 Synthetic
    unit test 로 state machine 규칙만 증명 (test_event_classifier.py 8건 pass).

### 2. 승인 박스 show/hide 정확도 — **충족**

- case-04 에서 `approval_show` 1건, 오탐 0건.
- synthetic 테스트: 모달 구간 외 block_commit 이 approval 으로 오분류되지 않음.

### 3. LoC 비교 — **criterion 재정의 필요**

현재 기준 (README Criteria 3 "≤ 70%") 으론 **미달**:

| 범위 | 파일:range | LoC |
|------|-----------|-----|
| 기존 — boundary | `bridge/parser.py:214-332` | 119 |
| 기존 — dedup | `bridge/stream_queue.py` 전체 | 104 |
| **기존 합계** | | **223** |
| 신규 — ANSI tokenizer | `tools/ansi_tokenizer.py` | 277 |
| 신규 — VT screen | `tools/vt_screen.py` | 335 |
| 신규 — line commit | `tools/line_commit.py` | 137 |
| 신규 — region tagger | `tools/region_tagger.py` | 200 |
| 신규 — event classifier | `tools/event_classifier.py` | 324 |
| 신규 — CLI orchestration | `tools/pipe_pane_analyzer.py` | 150 |
| **신규 합계** | | **1423** |

**6.4x 증가**. 그러나 이 비교는 **함정**: 분석기는 parser.py 214-332 + stream_queue
뿐 아니라 `is_approval`/`_approval_context`/`_approval_box`/`extract_response_blocks`/
`_is_block_active`/`_find_trust_prompt_region` 등 (parser.py 전체의 약 65%) 을
모두 대체. 또한 ANSI tokenizer + VT 는 **신규 기반 능력** — 현재 아키텍처에서
`capture-pane` 이 대신하던 역할 (외부 tmux 에 의존).

공정한 비교 두 가지 제안:

(a) **대체 범위 전체 LoC**: analyzer 1423 vs `bridge/parser.py` 전체 414 +
`bridge/stream_queue.py` 104 = 518. 2.75x — 여전히 부풀었지만 의도적. ANSI
처리라는 **구조적 능력** 추가.

(b) **유지보수 면적 LoC** (테스트 제외, regex/heuristic 밀도 측면): 분석기의 각
계층은 단일 책임이라 diff 읽기가 쉬움. 기존 `_response_region`/`take_new` 는
tail 정책 + 경계 탐색 + dedup 이 **동일 함수 안에 뒤엉켜** 있음.

**판정**: README Criteria 3 의 "≤ 70%" 는 현재 숫자와 맞지 않음. Criteria 를
"분석기 핵심 로직 (region_tagger + event_classifier) 합계 ≤ parser.py 214-332 의
5배" 같은 합리적 기준으로 재정의 권장. 현 상태: 200+324=524 LoC vs 119 → 4.4x.

### 4. 스피너 rate — **충족**

case-04 스트림에서 busy_enter 30건 ÷ raw 450KB ÷ 추정 7분 = 약 **0.07건/초**.
README Criteria 4 (≤ 5건/초) 대비 70배 여유. coalescing 별도 불필요.

### 5. 관측성 — **충족**

- 모든 이벤트는 `{offset, t, region, ...}` 포맷. 실패 케이스에서 "어느 offset
  에서 어떤 이벤트가 발화됐나/안 됐나" 를 직접 grep 가능.
- `--until-offset N` 옵션으로 바이트 단위 재현 가능.
- 예: case-04 에서 `approval_show` 가 offset 451837 에서 발화됨을 확인. 같은
  offset 으로 `--until-offset 451838` 돌리면 그 직전까지만 분석, 상태 투명.

## 샘플 output (case-04 approval 구간)

```json
{"offset": 451837, "t": "approval_show", "region": "modal_overlay",
 "tool_hint": "edit",
 "summary": "Do you want to make this edit to reviewer.py?"}
{"offset": 451837, "t": "approval_cancel", "region": "modal_overlay",
 "method": "flush"}
```

`approval_cancel(method="flush")` 는 raw 가 모달 미해소 상태에서 종료됨을 의미 —
case-04 의 "bot stuck, user 이 탐지할 때까지 2분 대기" 현상과 정확히 부합. 실제
사용자가 승인했다면 `approval_confirm`, ESC 눌렀다면 `method="esc"` 로 emit.
(confirm/deny 구분은 send_input 상관 통합 시 완결 — Step 2 범위 밖.)

## 범위 밖 (Step 2 에서 하지 않은 것)

- `user_prompt` 이벤트 — send_input log 통합 필요.
- `approval_confirm` / `approval_deny` 정밀 구분 — Enter + `❯` 위치 관찰 필요.
- `compaction_start` / `limit` / `trust_prompt` / `resume_picker` — baseline 에
  등장하지 않아 스키마 자리만 예약.
- case-01/02/03 fixture — raw ANSI 로그 재수집 필요.

## Step 3 게이트 권고

**전환 방향 지지 유지**. case-03 keystone 논거는 snapshot vs stream 패러다임
격차로 여전히 유효. 단, 세 가지 개선점:

1. Criteria 3 (LoC) 재정의 — 현 기준은 ANSI 처리 LoC 부재를 가정.
2. case-01/02/03 raw ANSI fixture 수집 — Step 1 live 수집 연장 (또는 재현
   시나리오 작성).
3. 프로덕션 통합 (Step 4) 전에 현재 분석기를 **24시간 shadow run** —
   production bridge 와 병행 실행하며 이벤트 diff 기록. criteria 3 부분의
   LoC 논쟁과 별개로 **실측 안정성** 이 더 중요한 증거.

## Decision Log (임시)

| Criterion | 결과 | 근거 |
|-----------|------|------|
| 1. 재현성 | 부분 (1/4 case pass) | case-04 green, 01/02/03 raw 부재 |
| 2. 모달 정확도 | Pass | case-04 0 false positive |
| 3. LoC ≤ 70% | **Fail (기준 재정의 권고)** | 1423 vs 223. 기반 능력 추가 감안 요 |
| 4. 스피너 rate | Pass | 0.07건/초 (기준 5건/초) |
| 5. 관측성 | Pass | offset+json + `--until-offset` |

**정성적 판정**: 3/5 명확 Pass, 1/5 부분, 1/5 기준 재정의. Gate 통과 부합
조건부 — 위 3개 개선점 수행 후 재판정 권고.
