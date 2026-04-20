# 시니어 개발자 리뷰 — ESC flush 회귀

리뷰 모델: Claude Opus 4.7. 관점: **시니어 개발자 (긍정·비판 혼합)**. 중점: 제안 diff 의 사이드이펙트, 원 리포트가 놓친 케이스, 테스트 전략.

## 한 줄 평

> Fix 1/2 방향은 정확함. 단, Fix 1 의 **tail 60 재스캔** 이 `is_approval` 의 tail 60 정책과 "형태는 같지만 근거가 다른" 코드가 되어, 장기적으로 또 다른 분기 오탐을 만들 수 있다. Fix 3 은 **원안 그대로면 스팸 소스**이므로 조건 강화 필요.

## Fix 1 정밀 검토

### 긍정

- `is_approval(text)` 게이트로 경계 truncation 을 live 모달일 때만 수행 → 원인 직접 차단.
- 내부 tail 60 재스캔은 `is_approval` 이 혹시 false-negative 일 때의 2차 방어선. 좋음.
- 기존 divider 탐색 로직은 건드리지 않음 → 경계 B/C 무영향.

### 우려 5가지

**1. 중첩 모달 — tail 60 안에 "Do you want to proceed" 2개**

시나리오: 사용자가 승인 모달을 ESC 로 닫은 직후 Claude 가 새 도구 호출로 다시 모달 띄움. tail 60 안에 구 모달(ESC 닫힘, scrollback 잔존) + 신 모달(라이브) 이 공존.

- `is_approval(text) == True` (tail 에 ❯ 1. Yes 가 있음) → 게이트 통과
- `range(len-1, tail_start-1, -1)` 역방향 첫 매칭 → **가장 최근** (= 라이브) 모달의 "Do you want to proceed" 를 잡음 → 올바른 경계
- 단, 역방향 스캔 전제 파괴 시(예: divider 가 구 모달 위에도 있으면) 오탐 가능

**대응**: test-plan.md 의 T2 NESTED_APPROVAL_PANE 로 커버 필요. 누락되면 이 케이스에서 재발.

**2. `is_approval` false-negative 의 파급**

`is_approval` 이 false-negative 면(라이브 모달인데 못 알아봄):
- Fix 1 게이트 → 경계 truncation 스킵 → 승인 박스 아래 텍스트도 응답으로 flush
- **동시에** core.py:438-460 의 approval 감지 분기도 False → pre-approval flush 스킵
- 결과: 실제로 승인 버튼 Telegram 전송 안 됨 + 이상한 응답이 흘러감

**다행히** 이는 Fix 1 의 퇴보가 아님 — 기존 대비 monitor 가 동일하게 실패(`is_approval=False` 면 원래도 approval 처리 안 됨). **새로운 실패 모드는 아니므로 본 작업에서 추가 대응 불필요**. 다만 `is_approval` 의 tail 60 이 얼마나 견고한지는 별도로 감사 대상.

**3. stale `end` 값 재계산 문제?**

우려: `is_approval` 이 False 면 경계 A 가 안 당겨짐. 그런데 `_response_region` 은 그 뒤 경계 B/C 로 `end` 를 재검토. 혹시 "이전 tick 에서 확정된 `end`" 가 남아있지 않나?

**확인**: `_response_region` 은 순수 함수. 매 호출마다 `end = len(lines)` 로 초기화(parser.py:219) 후 조건부로만 `min(end, i)`. **이전 tick 의 상태가 샐 경로 없음**. 문제 아님.

**4. 좁은 터미널 (<80 컬럼)**

divider 가 10자 미만이면 `_INPUT_DIVIDER_RE` 미매칭 → `is_approval` false-negative. 
- 현재 `_INPUT_DIVIDER_RE` (parser.py:26) 는 `─` 를 10 이상 요구.
- Claude Code 가 렌더하는 divider 는 훨씬 김(50+) — 실용적으로 문제 없음.
- 다만 설정 파일에 `min_divider_len` 상수로 뽑아두면 안전.

**5. 기존 `tests/test_bug_20260418_010300.py` 회귀**

이 테스트는 Welcome 배너 경계(경계 C) 검증. Fix 1 은 경계 A 만 건드림 → **회귀 없음**. 확증 필요: `python -m pytest tests/test_bug_20260418_010300.py -v`.

### Fix 1 결론

- 핫픽스로 승인. 단, **T1(stale) + T2(nested) 두 테스트를 반드시 포함**해야 장래 재발 시 즉시 red.
- 내부 tail 60 재스캔은 유지(방어선). 단, 2개월 후 `is_approval` 견고성 재감사 시 단순 `if is_approval(text):` 만 남기는 것도 고려.

## Fix 2 정밀 검토

### 긍정

- `cmd_esc` 에 상태 복원 로직 추가. monitor 의 sleep 을 깨우는 가장 직접적인 수단.
- `_log("USER-ACK", ...)` 태그가 receiver.py 의 다른 ACK 로그와 일관 → 로그 grep 시 혼란 없음.

### 우려 3가지

**1. `queue.reset()` 을 안 하는 게 정말 맞나?**

원 리포트가 reset 하지 않음. 이유: "같은 turn 내에서 Claude 가 이어서 출력할 수 있으므로 queue 좌표 유지".

**확인**: `/esc` 후 Claude 가 이어서 출력 → monitor 가 `awaiting_approval=False` 로 돌아가 `_flush_completed` 수행. 기존 `queue.idx/last_fp` 가 유효해야 다음 블록이 정상 take_new.

- ESC 직전의 `queue.idx` 는 이미 승인 박스 **앞**의 블록들이 모두 송출된 상태 (pre-approval flush 이후). 
- ESC 후 Claude 가 추가 블록 N 개 생성 → `completed = old_blocks + new_N_blocks` → `take_new` 가 N 개 새 블록 반환 → 정상.
- slip 감지: `last_fp` 가 완전 매칭 → 정상 복구.

**idle race 우려**: ESC 직후 사용자가 **새 메시지** 전송 → `on_message` 가 `bridge.queue.reset()` 수행 (receiver.py:462, 479) → 이중 reset 불필요. **race 없음 확증**.

**결론**: reset 생략 OK. 단, "Claude 가 ESC 직후 전혀 출력하지 않고 idle" 인 케이스는 flush 없음 — 정상.

**2. 늦은 승인 버튼과의 race**

시나리오: `/esc` → `awaiting_approval=False` 즉시 → 사용자가 "과거" 승인 메시지의 Yes 버튼을 누름 (UI 가 아직 업데이트 안 됨) → `approve_yes` 분기.

- `approve_yes` 는 `is_approval(clean)` 를 다시 체크 (receiver.py:172-190)
- ESC 로 모달 닫혔으므로 `is_approval=False` → late-approved 경로 (receiver.py:186)
- late-approved 는 Claude 에 `"사용자가 … 승인했습니다"` 텍스트 주입 — **의도치 않은 컨텍스트 오염**

**이게 새 리스크인가?** — 아니다. ESC 없이도 프롬프트 만료 시 동일하게 발생 가능(Claude 가 먼저 time-out). 기존 정책. Fix 2 로 **새로 생기는** 리스크 아님.

**근본 해결**: ESC 시 Telegram 승인 메시지를 `edit_message_text("❌ 취소됨")` + `reply_markup=None` 으로 즉시 무력화. **본 작업 범위 밖** — 후속 이슈로 기록.

**3. `/esc` 연타**

사용자가 `/esc` 를 2번 빠르게 누름 → `awaiting_approval=False` 가 2번 설정. **멱등** — 문제 없음. `send_key("Escape")` 도 2번 나가지만 이는 기존 동작과 동일.

### Fix 2 결론

- 핫픽스로 승인. `queue.reset()` 없는 선택은 분석상 안전.
- 단, T3 테스트에서 `queue.idx`/`last_fp` **불변**을 assertion 으로 박아야 장래 "친절한 누군가가 reset 추가" 하는 회귀를 방지.

## Fix 3 정밀 검토

### 원안의 문제

원 리포트의 로그 조건:
```python
if not new_blocks and log_tag == "response":
    _log("FLUSH-EMPTY", ...)
```

**정상 케이스에서도 찍힌다**:
- take_new 가 `[]` 리턴하는 경로는 (a) 이미 모든 블록 송출 완료, (b) 응답이 아직 안 찰, (c) slip + 앵커 미발견 — 3가지.
- (a), (b) 는 정상. 이 로그가 매 tick 찍히면 logs/ 오염.

### 리뷰 권고 (반영됨)

```python
if not new_blocks and log_tag == "response" and slip is not None:
    _log("FLUSH-EMPTY", ...)
```

- `slip is not None` → 앵커 복원 시도가 있었다는 뜻 = 진짜 의심 상황
- `log_tag == "response"` → busy-stream 도중의 정상 take_new=0 과 분리

**이게 원 리포트보다 나은가?** — 예. 노이즈 대비 시그널 비율 훨씬 좋음.

### 추가 우려

- dump.event 의 `flush_peek` 는 이미 완전 정보 보유 (`completed_count/new_count/slip/queue_idx`). Fix 3 은 **CB_DUMP=0 세션** 의 사각지대를 메우는 목적. CB_DUMP=1 세션에서는 포그라운드 로그 중복 — 의도된 용장성.
- 대안: `_log` 대신 `dump.event("core", "flush_empty_alert", ...)` 를 추가? → 이미 `flush_peek` 이 있고, CB_DUMP=1 세션에서만 볼 수 있는 지표. 핵심 목적(CB_DUMP=0 세션 관측)에 부적합. **기존안 유지**.

### Fix 3 결론

- 조건 강화본으로 승인. 테스트는 T5(선택) — `_log` 가 `print` 기반이라 `capsys`/patch 필요, 팀 컨벤션에 맞춰 추가.

## 원 리포트가 놓친 것

1. **중첩 모달** — 본 리뷰에서 추가. test-plan.md T2 로 반영.
2. **Fix 3 스팸 리스크** — 본 리뷰에서 추가. fixes.md 의 Fix 3 에 `slip is not None` AND 조건 반영.
3. **queue.reset() 생략의 race-free 확증** — 본 리뷰에서 명시적으로 분석. side-effects.md 에 기록.

## 반복 회귀의 근본 원인 (구조)

같은 파일 경로(`_response_region`)에서 이번에 3번째 회귀. 패턴이 고정적:

1. 경계 감지 함수가 pane 전체/넓은 tail 스캔
2. scrollback 에 과거 마커 잔존
3. 라이브성 판정 없이 마커를 신뢰 → 경계 오탐

**구조 개선 권고** (본 작업 범위 밖):

```python
# parser.py 에 추가
def boundary_from_live_box(text: str) -> tuple[int | None, str]:
    """라이브 박스 상단 경계. 없으면 (None, "")."""
    if is_approval(text):
        return _approval_boundary(text), "approval"
    if is_trust_prompt(text):
        return _trust_boundary(text), "trust"
    if is_resume_picker(text):
        return _resume_boundary(text), "resume"
    return None, ""
```

- 모든 라이브 박스 판정이 한 곳. 새 박스 종류 추가 시 이 함수만 업데이트.
- `_response_region` 은 `idx, kind = boundary_from_live_box(text)` 한 줄로 경계 A 대체.

2주짜리 작업. 다음 분기 착수 권고.

## 최종 평가

- Fix 1/2/3: 핫픽스로 승인, 지금 반영.
- 테스트 T1-T4 필수, T5 선택.
- 구조 개선(헬퍼 통합)은 별도 작업. 긴급성 저, 중요도 고.

## 참고

- 구체 diff: [../../plans/20260418-esc-flush-recovery/fixes.md](../../plans/20260418-esc-flush-recovery/fixes.md)
- 사이드이펙트: [../../plans/20260418-esc-flush-recovery/side-effects.md](../../plans/20260418-esc-flush-recovery/side-effects.md)
- 테스트 상세: [../../plans/20260418-esc-flush-recovery/test-plan.md](../../plans/20260418-esc-flush-recovery/test-plan.md)
