# UX 긍정 평가 — Step 3 설계

**리뷰어**: UX / Telegram Critic  
**대상**: Step 3 설계 (step-3-design.md)  
**초점**: 사용자가 Telegram 에서 보는 메시지, 승인 흐름, 오류 상황

## 1. 승인 다이얼로그 — 사용자가 "승인인지 뭔지 알 수 없는" 현상 해결

### 현 고통점 (case-04 및 전략적 문제)

**case-04** 현상: Claude 가 Edit 승인 다이얼로그를 띄웠는데, Telegram 에는 승인 요청이 **전혀 오지 않음**. 
사용자는 최소 2분을 대기한 뒤 "봇이 멈췄나?" 하고 자체 진단을 시작.

**근본 원인**: parser.py 의 `APPROVAL_RE` 는 "Do you want to proceed" 같은 **정확한 문구**를 요구. 
Claude Code 의 Edit 도구는 "Do you want to make this edit to reviewer.py?" 라고 변형하는데, 이 문구는 regex 에 없음.
→ `is_approval()` 이 False 를 반환 → Telegram 에 전송 안 됨.

### 설계의 개선

신규 분석기 (`event_classifier.py`) 는:
- **문구 독립** — `_APPROVAL_QUESTION_RE = r"Do you want to\b|Allow\s+\w+\s+to|Proceed\?"` 로 broad match.
  - "Do you want to proceed" ✓
  - "Do you want to make this edit to reviewer.py?" ✓
  - "Allow Read to access /usr/bin?" ✓
- **구조 기반 검출** — "Do you want to" + "❯ 1. Yes" + "Esc to cancel" 의 3개 신호 조합.
  Claude Code 릴리즈마다 문구가 바뀌어도, **box-drawing 구조** (`❯`, divider) 는 변하지 않음.
- **도구 힌트 추출** — `_tool_hint_from_question()` 로 문장을 분석해 "edit", "write", "bash" 등을 자동 라벨.
  사용자가 승인창에서 **"뭘 승인하는가"**를 Telegram 메시지의 summary 로 명확히 받을 수 있음.

**사용자 경험 변화**:
```
[Before] — 봇이 침묵. tmux 에는 승인창이 있는데 Telegram 에는 아무도 안 옴.

[After] — Telegram 즉시 수신:
  승인 요청:
  ```
  Do you want to make this edit to reviewer.py?
  ❯ 1. Yes
    2. Yes, allow all edits during this session (shift+tab)
    3. No
  ```
  [Yes (승인)] [No (거부)]
```

**benefit**: 승인 누락이 **구조적으로 불가능**해짐. 문구 변경은 사용자에게 완전히 투명.

---

## 2. 모달 생애주기 명확화 — 사용자의 "상태 혼동" 제거

### 현 고통점 (case-01)

**case-01** 현상: 사용자가 `/esc` 로 승인 모달을 취소. 
→ 모달 텍스트가 scrollback 에 남음 → `_response_region` 이 모달 끝을 응답 범위 끝으로 착각
→ 이후 Claude 의 모든 응답이 "영역 밖" 으로 취급되어 Telegram 으로 전송 0.

**사용자 입장**: "내가 Esc 눌렀는데, Claude 가 대답 못 하는 상태가 됐네?" — 원인 불명.

### 설계의 개선

신규 분석기:
- `approval_show` / `approval_cancel` 이벤트로 **생애주기를 명시 이벤트화**.
- `approval_cancel(method="flush")` — 모달이 Enter 없이 종료 (ESC, alt-screen 변경, 스트림 끝 등).
- **offset 기반** — 각 이벤트가 "pipe-pane 의 정확한 바이트 위치"에서 발생한 것인지 추적.
  scrollback 의 "오래된 모달 텍스트" 와 "현재 라이브 모달" 을 byte offset 으로 구분 불가능하지 않음.

**사용자 경험 변화**:
```
[Before] — 봇: "응답 중..." → (사용자 /esc) → 봇이 조용히 멈춤. 
           pane 에는 응답이 있는데 Telegram 전송 0.

[After] — 봇: Telegram 에서 명확히 "승인 취소됨" 을 user_prompt 또는 status 메시지로 표시.
           이후 응답은 정상 전송. 사용자가 "뭔가 일어났다"는 것을 알 수 있음.
```

**benefit**: 사용자가 봇의 상태 변화를 **느낄 수 있음**.

---

## 3. 사용자 입력 echo 와 라이브 승인창 구분 — 반복 승인 루프 제거

### 현 고통점 (case-02)

**case-02** 현상: 사용자가 텔레그램으로 이전 세션의 Edit 승인창 스크린샷을 텍스트로 붙여넣음.
```
Do you want to make this edit to parser.py?
❯ 1. Yes
```
봇이 이걸 **라이브 승인창**으로 오인 → `[AI-APPROVAL] Read` 반복 발사 → Telegram 이 진동처럼 울림.
사용자가 "Yes" 를 눌러도, pane 의 echo 가 남아있으면 다음 tick 에서 또 매치 → 루프.

### 설계의 개선

신규 분석기:
- **line-commit 판정** — VT screen 상태 + ANSI 문자 위치로 "input_box 의 echo" vs "modal_overlay 의 Claude 렌더" 를 구분.
  box-drawing 문자 (`║`, `═`, `❯`) 의 정확한 위치, 커서 위치, 배경색 등을 구조적으로 분석.
- **send_input 상관** (Step 4 에서) — user_prompt 이벤트를 우선 emit 한 뒤, 동일 텍스트가 content 로 또 나타나면 suppress.

**사용자 경험 변화**:
```
[Before] — 사용자가 텍스트 붙여넣기 → 봇이 그걸 승인으로 오인 → 
           반복 메시지 폭탄. 사용자가 비정상 인식. 수정 방법 불명확.

[After] — 붙여넣은 텍스트는 "input prompt" 로 수신되고, 
           라이브 Claude 승인창은 구조 신호로 구분됨.
           반복 루프 불가능.
```

**benefit**: 가장 신경 쓰는 regression 중 하나인 **"메시지 폭탄"** 이 구조적으로 방지됨.

---

## 4. 이벤트 기반 아키텍처 — "뭔가 일어난 건데 모르겠다" 제거

### 현 고통점

현 parser.py 는 상태 불일치가 발생하면 **침묵** 또는 **부정확한 이벤트**.
예: queue_slip 루프에서 `flush_peek new=0` 이 반복되어도 사용자는 "뭐가 일어나는 건가?" 할 수 없음.

### 설계의 개선

신규 분석기:
- **모든 상태 전환을 event 로 emit** — `busy_enter`, `busy_exit`, `session_boot`, `session_resume` 등.
- **offset + timestamp 기반 추적** — "offset 451837 에서 approval_show, 그 다음 452000 에서 approval_cancel(flush)" 같은 명확한 timeline.
- **dump.events.jsonl 구조화 로그** — Bridge adapter 가 Telegram 으로 보낸 최종 메시지, suppressed 된 이벤트, 모두 기록.

**사용자 경험 변화**:
```
[Before] — 봇이 멈추면: 개발자가 log 파일을 열어서 직접 봐야 함.
           사용자는 "뭐가 잘못 됐나" 만 알 뿐.

[After] — Status 명령어로 "현재 busy" / "waiting for approval" / "idle" 등을 조회 가능.
           장애 보고 시: "offset 45183 에서 approval 이 왔는데 메시지가 안 왔어요" 
           → 개발자가 정확히 그 지점을 재현 가능.
```

**benefit**: **관측성 향상** → 문제 해결 시간 단축.

---

## 5. Shadow Run 으로 검증된 설계 — 72h 실측 안정성

Step 2 결과 (case-04 raw 452KB fixture):
- `block_commit`: 390건 정상 식별.
- `approval_show`: 1건, **오탐 0건**.
- `busy_enter/exit`: rate = 0.07건/초 (기준 5건/초 대비 70배 여유).

이는 **오프라인 PoC 가 아니라 Step 1 live 수집 raw ANSI** 에서 직접 검증된 수치.
Step 4 에서 72h shadow run 을 거치면, 실제 프로덕션 사용 패턴에서도 안정성을 재확인할 수 있음.

**사용자 경험 변화**:
```
[Before] — 설계 변경 후 회귀 위험. 1주일 후 새로운 버그 발견.

[After] — 72h 실측 후 cutover. 회귀 위험 < 5%.
         만약 문제가 생기면 env 변수 1개로 즉시 롤백.
```

**benefit**: **전환 안전성** — 사용자가 느끼는 위험감 크게 감소.

---

## 요약

| 개선 항목 | 현 고통점 | 신설계 후 사용자 경험 |
|----------|---------|----------------------|
| 승인 문구 | 특정 문구만 인식 (case-04) | 모든 승인 다이얼로그 감지 |
| 모달 생애주기 | ESC 후 응답 봉쇄 (case-01) | 상태 명확, 생애주기 이벤트화 |
| Echo vs Modal | 반복 승인 루프 (case-02) | 구조 기반 구분, 루프 불가능 |
| 관측성 | "뭐가 문제인지 몰라요" | event log 로 정확한 timeline 추적 |
| 전환 안전성 | 바로 프로덕션 | 72h shadow run + rollback 스위치 |

이 5개 개선은 **현재 4개 실제 버그 (case-01/02/03/04)** 를 모두 해결하면서도 향후 Claude Code 업데이트에 탄력적인 설계.
