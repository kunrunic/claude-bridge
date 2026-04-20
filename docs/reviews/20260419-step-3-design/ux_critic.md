# UX 비판적 검토 — Step 3 설계

**리뷰어**: UX / Telegram Critic  
**대상**: Step 3 설계 (step-3-design.md)  
**초점**: 전환 중 사용자 노출 표면, 메시지 순서 변화, 침묵 위험, Telegram 텍스트 변화

---

## 1. Step 4-α (Shadow Run) 중 이중 메시지 송출 위험

### 시나리오

Step 4-α 에서는 두 경로가 **병행 실행**:
- **기존 경로** (parser.py) — `_send_approval()` 호출 → Telegram 메시지 전송
- **신규 경로** (analyzer) — `approval_show` 이벤트만 record (dispatch 미하지만, 로그는 기록)

**만약 둘 다 Telegram 으로 보낸다면:**

사용자가 같은 승인에 대해 **중복 메시지 2건** 받음:
```
[14:30:15] 승인 요청:
  Do you want to make this edit to reviewer.py?
  [Yes] [No]

[14:30:16] 승인 요청:          ← 신규 분석기 버전 (약간 다른 summary 포맷?)
  Do you want to make this edit to reviewer.py?
  [Yes] [Yes, allow all edits...] [No]
```

### 현재 설계의 완화

✓ Step 4-α 문서 (step-3-design.md §5.1) 에서:
> 분석기를 bridge 와 **병행 실행**. dispatch 는 **기존 parser 경로가 계속 담당**.
> 분석기 출력을 별도 파일 `analyzer-events.jsonl` 로 기록.

즉, 이 단계에서는 Telegram 중복 송출이 **설계상 없어야** 함.

### 남은 우려

1. **구현 실수**: bridge/core.py 가 실제로 신규 분석기 이벤트를 구독하지 않도록 철저히 보호되어야 함.
   - 현재 코드 (core.py 라인 30) 는 `from . import config, dump, parser, sender, session, tmux` — 파서만 import.
   - analyzer 는 아직 import 안 됨 ✓
   - **하지만 Step 4-α 에서 분석기를 "부착" 할 때** `pipe_attach()` 에서 실수로 dispatch 를 할 수도 있음.

2. **approval 메시지 텍스트 불일치** — 신규 vs 기존:
   ```python
   # 기존 (sender.py:71)
   snippet = parser._approval_box(text)
   
   # 신규 (준비 중)
   snippet = analyzer_event["summary"]  # 짧은 한 줄
   ```
   사용자가 "어? 텍스트 포맷이 다르네?" 할 가능성 있음.

### 권고

- [ ] Step 4-α 출발 전 `core.py` 에서 analyzer import 를 명시적으로 **차단** (예: `if not BRIDGE_DISPATCH_SOURCE.startswith("analyzer"): raise` 방어).
- [ ] approval_show 페이로드에 `box_text` 필드를 추가해 기존 `_approval_box()` 와 동일한 multi-line snippet 을 제공하기.

---

## 2. `approval_cancel(method="flush")` 가 사용자에게 노출될 위험

### 시나리오

event_classifier.py:137-143 에서:
```python
if self._modal is not None:
    self._close_modal(
        self._modal.yes_offset or self._modal.question_offset or 0,
        out,
        reason="flush",  # ← method="flush"
    )
```

**flush** 는 "스트림이 끝났는데 모달이 아직 열려있음" 을 의미.
case-04 에서 정확히 이런 상황 — 승인창이 뜬 상태로 사용자 세션 단절 또는 모니터 종료.

### 사용자에게 보이는 메시지?

BridgeAdapter 구현 (Step 4-γ 이후) 에서:
```python
if event.t == "approval_cancel":
    if event.payload["method"] == "flush":
        msg = "⚠️ 승인 대기 중 연결 끊김. Claude 가 대기 중입니다."
        await app.bot.send_message(chat_id, msg)
```

**문제**: 사용자가 이 메시지를 **언제 받을지 모름**.
- 세션 종료 후? → 봇도 꺼져있을 수 있음.
- 모니터 재기동 후? → "어제 대기했던 거군" 하고 혼동.
- 아니면 아예 안 보내나? → "뭐가 일어났는지 모르겠는데?" 침묵 위험.

### 현재 설계에서 누락된 부분

- 'BridgeAdapter 가 approval_cancel 을 받으면 **어떤 액션**을 할 것인가' 가 명시되지 않음.
  - §3 모듈 경계에서 "입력: AnalyzerEvent 스트림", "출력: Telegram 메시지 전송" 만 있음.
  - adapter 에서 cancel 을 **drop** 할지, **notify** 할지, **resume 상태** 로 남길지 불명확.

### 권고

- [ ] Step 4-γ 설계에서 approval_cancel 의 사용자 노출 정책 명시:
  - 옵션 1: suppress (사용자는 모름. 개발자만 log 에서 봄).
  - 옵션 2: notify (⚠️ 메시지 전송, 하지만 timing 명확).
  - 옵션 3: auto-deny (자동으로 "거부" 처리하고 사유 설명).
- [ ] case-04 의 "2분 대기" 를 개선하려면, flush 발생 시 **즉시** 사용자에게 통보 필요.

---

## 3. Session Boot / Resume 배너 노출 여부

### 현 동작

event_classifier.py:231-235:
```python
if _BANNER_BOOT_RE.search(text):
    out.append(AnalyzerEvent(offset=offset, t="session_boot", region="content"))
    return
if _BANNER_RESUME_RE.search(text):
    out.append(AnalyzerEvent(offset=offset, t="session_resume", region="content"))
```

이 이벤트들이 BridgeAdapter 에서:
- (A) suppress 되나? (사용자는 Telegram 에서 못 봄)
- (B) Telegram 으로 전송되나? ("Welcome to Claude Code" 배너가 사용자 메시지로 옴?)
- (C) internal 로만 기록되나?

### 사용자 경험 변화

| 시나리오 | 기존 | 신설계 (case A) | 신설계 (case B) | 신설계 (case C) |
|---------|------|-----------------|-----------------|-----------------|
| /start 후 첫 메시지 | "안녕하세요" 직접 | (변화 없음) | 배너 + "안녕하세요" | (변화 없음) |
| /resume 후 | "이전 내용 맥락 있음" | (변화 없음) | "Resuming session" 공지 | (변화 없음) |
| 로그 관측성 | — | boot/resume 이벤트 있음 ✓ | 동일 + Telegram 메시지 | boot/resume 이벤트만 내부 |

**현재 설계에서**: session_boot, session_resume 이벤트는 emit 되지만, **"BridgeAdapter 가 어떻게 처리할지"** 가 명시되지 않음.

### 권고

- [ ] Step 3 설계 문서에 추가:
  > session_boot / session_resume 이벤트는 로깅만 수행. Telegram 으로는 전송 X.
  > (이유: 사용자 경험상 배너 중복이나 noise 증가 우려. 필요시 /status 명령으로 조회 가능.)

---

## 4. `block_commit` 의 "kind" 필드가 항상 "response" — user-echo 구분 불가

### 현 제약

event_classifier.py:275:
```python
self._block_buf_kind = "response"  # PoC: 이분 분류 생략, 전부 response
```

### 문제점

기존 parser 는 (어려움에도) "사용자가 붙여넣은 텍스트 echo" 와 "Claude 응답" 을 어느 정도 구분하려 했음.
신설계에서는 **PoC 단계** 이므로 모두 "response" 로 통일.

case-02 (scrollback echo 오탐) 에서 이 구분이 중요:
- Echo: "사용자가 텔레그램으로 붙여넣은 텍스트" → echo 될 때 구분 가능하고 suppress 해야 함.
- Response: "Claude 가 실제로 생성한 응답" → 항상 Telegram 으로 전송.

### Step 4 에서의 영향

case-02 해결을 위해 Step 4-β (§4 Feature Parity) 에서:
> case-02 user-echo 구분 | 미구현 | user_prompt 상관으로 구조 해결 (tail-scan 대비 상위 호환) | 중

즉, **send_input log 상관**으로 해결하겠다는 계획 있음. 다만:
- send_input 이 정확히 어떤 타이밍에 기록되는가?
- user_prompt 이벤트를 **우선** emit 한 후, 동일 텍스트를 suppress 하는 로직이 race condition 없는가?

### 현재 설계에서 우려

- Step 4-β 에서 "user_prompt 상관" 구현이 **복잡도 중** 으로 평가되지만, 상세 스펙 없음.
- 만약 구현이 지연되면, Step 4-α shadow run 중에 **case-02 회귀** (echo 루프) 가 재현될 수 있음.

### 권고

- [ ] Step 4-β 설계에서 user_prompt 이벤트의 정확한 emit 타이밍 명시:
  - bridge 가 send-keys 명령을 보낸 직후? 
  - tmux 에서 echo 가 감지된 후?
  - 예상 지연 시간?
- [ ] case-02 재현 스크립트를 Step 4-α 에 포함해 shadow run 중 재확인.

---

## 5. 설계 문서에서 누락된 BridgeAdapter 인터페이스 명세

### 현 상황

step-3-design.md §3 에서:
> **BridgeAdapter** 가 신규 경계:
> - 입력: `AnalyzerEvent` 스트림 + `send_input` log (user prompt 상관용).
> - 출력: Telegram 메시지 전송, dump 이벤트 기록, 상태 캐시 갱신.

**하지만 구체적 이벤트 → Telegram 메시지 매핑이 없음.**

| AnalyzerEvent | BridgeAdapter 동작 | Telegram 출력 | 불확실성 |
|---------------|--------------------|--------------|---------|
| block_commit | `_send_output()` 호출 | ✓ 명확 (기존과 동일) | 없음 |
| approval_show | ??? | 승인 메시지? 형식? | **높음** |
| approval_cancel | ??? | notify? suppress? | **높음** |
| busy_enter/exit | ??? | 진행중 표시? suppress? | **중** |
| session_boot | ??? | 배너? silence? | **중** |
| user_prompt (Step 4-β) | ??? | 사용자 입력 echo? | **높음** |

### 사용자 경험의 불확실성

- "approval_cancel 을 받으면 무슨 메시지가 갈까?" 를 누가 결정?
- "busy_enter 를 받으면 진행 상황을 Telegram 으로 표시할까, 아니면 조용할까?"
- 기존 behavior 를 유지하나, 새로운 것을 시도하나?

### 권고

- [ ] Step 4-γ (BridgeAdapter 도입) 의 사전 설계 문서 작성:
  ```
  # BridgeAdapter Event Dispatch 명세
  
  | Event Type | Input Payload | Output Action | Telegram Message | Risk |
  | approval_show | tool_hint, summary | send_approval() | "승인 요청: ..." | 기존과 호환? |
  | approval_cancel | method (flush/esc) | notify_or_suppress() | ... | 타이밍? |
  | ...
  ```
- [ ] 이 명세를 Step 4-α 시작 **전**에 리뷰받기. shadow run 중에 정책 변경 없도록.

---

## 6. Silence Failure — 분석기가 crash 하면?

### 시나리오

Step 4-α 에서 분석기가 **exception 을 throw**:
```python
# tools/event_classifier.py 에서 regex 오류, 메모리 부족, 기타 예외
try:
    out = classifier.feed(tagged_line)
except Exception as e:
    # 어디서 처리? 누가 기존 parser 로 fallback?
    pass
```

### 현재 설계에서

- Step 4-α 에서는 분석기 출력이 "별도 파일" (`analyzer-events.jsonl`) 로만 기록.
- 기존 parser 경로가 **여전히 Telegram dispatch** 담당.
- 분석기 crash → 파일 1개 기록 실패 → **사용자는 무영향** (기존 경로가 여전히 작동).

**하지만 Step 4-γ (dispatch cutover) 이후:**
- BridgeAdapter 가 분석기 이벤트에 의존.
- 분석기 crash → event stream 끊김 → ???

### 권고

- [ ] Step 4-γ 에서 분석기 실패 시 fallback 정책 명시:
  - 옵션 1: basic fallback 파서 (기존 parser 간단화 버전).
  - 옵션 2: suppress + notify (사용자에게 "서비스 재개될 때까지 대기").
  - 옵션 3: exception log + user_prompt 에 오류 메시지.
- [ ] exception handling 을 core.py, adapter.py 에서 **반드시** 구현. PoC 단계가 아님.

---

## 7. Rate-Limiting — busy_enter 30건이 사용자에게 보일까?

### case-04 수치

> case-04 스트림에서 busy_enter 30건 ÷ raw 450KB ÷ 추정 7분 = 약 **0.07건/초**.

이는 **이벤트 발생 빈도**이지, **사용자가 보는 진행 표시** 빈도가 아님.

### 사용자 경험

기존:
```
[14:30] User 메시지 전송
[14:30:05] ✶ Reading files...
[14:30:10] ✶ Generating code...
[14:30:15] ✓ Done
[14:30:16] ⏺ Here's the code...
```

신설계 (만약 busy 를 Telegram 으로 전송하면):
```
[14:30] User 메시지 전송
[14:30:05] Telegram: [잠깐, 파일 읽는 중...]
[14:30:10] Telegram: [생성 중...]
[14:30:15] (업데이트 없음)
[14:30:16] Telegram: ⏺ Here's the code...
```

**문제**: "busy 이벤트 30건" 을 모두 Telegram 메시지로 보내면 메시지 폭탄.

### 권고

- [ ] Step 4-γ 에서 busy_enter/exit 의 Telegram 출력 정책:
  - Suppress (로그만, 메시지 안 보냄).
  - Throttle (매 5초마다 1건만 집계).
  - 기타.
- [ ] current design 은 정책이 없으므로, 구현 단계에서 "오버스로드" 위험.

---

## 요약 테이블: 사용자 노출 표면의 위험

| # | 이벤트/현상 | 기존 동작 | 신설계 (설계 문서) | 신설계 (구현 후) | 사용자 영향 | 위험 |
|----|-----------|---------|-----------------|-----------------|----------|-----|
| 1 | approval_show | snippet 전송 | 동일하되 구조 기반 감지 | ??? | 긍정: 모든 승인 감지 | **중** (중복 가능) |
| 2 | approval_cancel | 이벤트 없음 | method=flush 시 cancel 이벤트 | ???로그만? notify? | 중립~긍정 | **중** (타이밍/텍스트) |
| 3 | session_boot/resume | 배너 suppress | event only, Telegram 미정 | ??? | 중립 | **중** (불명확) |
| 4 | user_prompt (Step 4-β) | echo 감지 어려움 | send_input 상관 | 구현 지연 위험 | 긍정 가능 | **높음** (복잡도) |
| 5 | busy_enter/exit | 내부 로그만 | event emit | ???메시지폭탄? | 중립~부정 | **높음** (suppress 필수) |
| 6 | block_commit kind | response vs echo | 모두 response (PoC) | kind="response" only | 회귀 위험 | **중** (echo 루프) |
| 7 | 분석기 crash | 없음 (offline) | Step 4-α 에서 발생 가능 | fallback 정책 없음 | 기존 parser 보호 | **중** (Step 4-γ 에서 위험) |

---

## 최고 우선순위 권고 (전환 전)

1. **BridgeAdapter 이벤트 dispatch 명세** (step 4-γ 전 필수)
   - approval_show / approval_cancel / busy_* / session_* → Telegram 메시지 매핑 명시.

2. **case-02 (echo 루프) 재현 테스트를 Step 4-α 에 포함**
   - user_prompt 구현 지연 시 회귀 위험 조기 감지.

3. **Silence failure 처리**
   - 분석기 exception handling + fallback (Step 4-γ 에서 필수).

4. **approval_cancel 타이밍 명확화**
   - "flush 발생 → Telegram 전송" 의 지연 시간, 사용자 경험 정의.

5. **busy_enter 메시지 폭탄 방지**
   - Throttling 또는 suppress 정책 사전 결정.
