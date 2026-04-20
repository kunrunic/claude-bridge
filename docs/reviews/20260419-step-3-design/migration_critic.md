# Migration 안건 — Step 3 설계 위험 재점검

## 요약

설계는 단계적 가역성을 잘 갖추고 있으나, **폐기 정당성** 과 **shadow run 의 한계** 에서 세 가지 중대한 약점이 노출된다:

1. 기존 parser.py 의 핵심 로직 중 "구조적 대체" 불가능한 부분이 있는가?
2. 72h shadow run 은 충분한가? 어떤 증상을 놓치는가?
3. rollback 환경변수가 실제로 작동하는가? (공유 상태 오염 시)

---

## 1. 폐기 정당성 스트레스 테스트

### 1.1 `_response_region` 경계 탐색 폐기 → "VT row 가 직접 주어짐" 근거 재검토

**설계 주장** (§2, §1):
> VT row 가 직접 주어지므로 본질적 가치가 사라진다. 최소 변경 재사용은 "두 paradigm 공존" 을 만들어 유지보수 비용이 2배.

**폐기 항목**: parser.py:214-257 (63 LoC), 특히:
- 경계 A: 승인 박스 위 divider 탐색 (tail 제한 없음).
- 경계 B: 마지막 15줄 내 입력창 divider (Welcome 배너 오탐 방지 로직).
- 경계 C: Fresh 부팅 배너 vs 세션 재시작 배너 구분 (has_content_above 조건).

**스트레스 테스트**:

1. **경계 C (Welcome 배너) 구분 로직은 구조적 대체 불가능**:
   - 기존: `has_content_above` 조건으로 "Fresh 부팅(맨 위) vs 중간 restart" 판정.
   - 분석기: VT row 를 받지만, **raw ANSI 스트림에서 중복 부팅 배너를 구분할 수 있는가?**
   - 실제 시나리오: 세션이 3회 restart 되면 welcome 배너가 3개 스택. scrollback 에서만 "이전 restart 배너" 와 "현재 재시작 배너" 구분 가능.
   - **문제**: analyzer 는 VT 상태 머신이므로 "과거 배너가 언제 렌더되었는가" 의 time-series 정보 없음. offset 으로만 판정 → 편향된 row 선택 가능.
   
   **설계가 제시한 대응**: "VT row 가 직접 주어짐" 이지만, **다중 재시작 후 정확한 "응답 영역" 경계를 어떻게 결정하는가가 미명시**.
   
   📌 **필수 설계 변경**: region_tagger 에서 multi-boot 시나리오(case-05로 추가) 의 경계 판정 규칙 명시. 단순 row 기준은 불충분.

2. **경계 B (입력 divider 15줄 제한) 는 "유지보수 면적" 으로는 개선이지만 정확도는 미검증**:
   - 기존: tail 15줄 안에서 divider 찾음 → 매우 국소적.
   - 분석기: VT 상태가 "현재 어떤 row 가 input box 이상인가" 를 알아야 함 → 더 복잡한 heuristic 필요.
   - **실제 위험**: 터미널이 매우 길면 (y=200 이상) input divider 가 화면 중간 어딘가에 있을 때, VT row 만으로는 "화면 끝에서 가장 먼저 나타난 divider" 와 "실제 입력 박스 divider" 를 구분 불가.
   
   **현 설계 근거**: 설계 §6 "현재 450KB 처리 < 1s. 온라인은 chunk 단위 (≤ 64KB) 증분" → **perf 은 보장하지만 correctness 는 실측 필요**.

   📌 **필수 설계 변경**: shadow run 에서 "터미널 높이 변경 후 divider 오탐율" 을 명시적으로 추적.

3. **경계 A (tail 제한 없는 승인 박스 탐색) 폐기 ≠ 실제 폐기**:
   - 설계 §2 에서 "경계 A 폐기" 라고 했으나, 실제로는 region_tagger 에서 **동일한 로직 재구현** 필요.
   - 기존 parser.py: "전체 역방향 탐색" → ESC 로 닫힌 stale modal 까지 탐색해 경계 truncation.
   - 분석기: envelope 구조 (divider + ❯ 1. Yes + footer) 로만 판정.
   - **구조적 차이**: parser 는 **문자열 매칭** (tail 정책 비대칭), 분석기는 **structural parsing** (divider 체크).
   
   ✅ **실제로는 상위호환**: 문자열 매칭보다 구조 기반이 더 정확 (step-2-results case-04 증명).
   
   하지만 **"폐기" 가 아니라 "재설계"** — 의미상 차이.

---

### 1.2 `StreamQueue.idx/last_fp` dedup 폐기 → "offset+t tuple 이 구조적 dedup 키" 근거 재검토

**설계 주장** (§2 row 2):
> `StreamQueue.idx/last_fp` 는 폐기. `(offset, t)` tuple 이 구조적 dedup 키.

**폐기 항목**: stream_queue.py 전체 (104 LoC).
- `idx`: 다음 송출 시작 인덱스.
- `last_fp`: 마지막 송출 블록 앵커.
- `detect_slip()`: scrollback eviction 감지.
- `take_new()`: slip 발생 시 앵커 재탐색.

**스트레스 테스트**:

1. **Scrollback rotation 감지 손실 가능**:
   - 기존: `idx > len(completed)` 또는 `completed[idx-1].anchor != last_fp` → eviction 감지 → log + recover.
   - 분석기: `(offset, t)` tuple 로 dedup → **tuple 의 uniqueness 는 확보되지만, eviction 이 발생했는지는 어떻게 알 수 있는가?**
   - **시나리오**: pipe-pane 파일이 rotate 되고 새 파일이 offset 0 부터 시작 → 분석기는 "새로운 (0, t) tuple" 로 보면 자연히 dedup. 근데 **사용자는 모름** (로그 없음).
   
   **설계가 제시한 대응** (§6): "inode 감지 (이미 Step 1 rotate 존재)".
   - 문제: inode 감지는 **파일 시스템 레벨**. 분석기의 offset-based tuple 은 논리 레벨 → **계층 분리 필요**.
   - 구체적 구현: 분석기가 rotate 를 감지하려면 "이전 파일의 마지막 offset" + "현재 파일의 시작 offset" 을 비교해야 함. 설계에서 명시하지 않음.
   
   📌 **필수 설계 변경**: Step 1 의 rotate 감지 + Step 4 의 분석기 rotate recovery 를 **명시적으로 연결**. 현재는 "Step 1 이 이미 존재" 라는 수동 연결만 있음.

2. **Eviction 후 "얼마나 손실되었는가" 정보 손실**:
   - 기존: `detect_slip()` 로그 → "idx 가 completed 를 초과했다" / "앵커 불일치" 신호 → 얼마나 심한 손실인지 추정 가능.
   - 분석기: tuple dedup 만으로는 **"놓친 블록이 몇 개인가" 를 알 수 없음**. 오직 inode 변경으로만 감지.
   
   **현 설계 근거** (§6): "offset-based resume 가능. 재기동 시 마지막 커밋된 offset 부터 재생".
   
   → 손실되지 않음. 하지만 **shadow run diff 기간에 "누락" 을 포착하지 못할 수 있다**.
   
   **예**: 파일 rotate 후 분석기가 offset 0 부터 재시작 → 논리적으로 올바름 → 하지만 기존 parser 는 여전히 "lost 9개 블록" 로그 → diff 에서 보임.
   
   → 이것은 **정상 회귀** (rotate 시 offset 리셋)가 아니라 **설계 의도 차이** (구조적 dedup vs 위치 기반 추적).
   
   📌 **필수 설계 변경**: shadow run 에서 rotate 이벤트를 명시적으로 mark → diff 계산 시 rotate 경계를 제외하거나, rotate 발생 시 diff 리셋.

3. **"구조적 dedup" 과 "실제 보낸 블록" 의 loose coupling**:
   - 기존: `last_fp` 로 "정확히 이 블록까지 보냈다" 를 추적 → 스트림 성능과 추적 정확도 결합.
   - 분석기: offset+t tuple 로 dedup → **별도의 "실제 전송 로그" 와 양방향 검증 필요**.
   
   **설계가 제시한 대응** (§8): dump.events() 에 "bridge.dispatch" 와 "bridge.suppress" 이벤트 기록.
   
   → 이론상 충분하지만, **shadow run 은 parser 와 analyzer 를 병행 실행하는 것이므로, 두 경로의 "실제 전송 이벤트" 를 정확히 diff 계산하려면 메타데이터 일관성이 필수**.
   
   📌 **필수 설계 변경**: shadow run diff 계산식을 구체적으로 명시. "block_commit ±5%" 는 분석기 이벤트만 보는 것인가, 아니면 "bridge.dispatch 이벤트를 포함한 최종 비교" 인가?

---

### 1.3 `is_approval` tail-scan 폐기 → "envelope 구조 검사가 상위호환" 근거 재검토

**설계 주장** (§2 row 3):
> `is_approval` tail-scan 은 폐기. envelope 구조 검사가 상위 호환.

**폐기 항목**: parser.py:101-123 (23 LoC).
- tail 60줄 제한.
- 3단 구조: ❯ 1. Yes + 위 15줄 내 Do you want to + Yes 아래 divider 부재.

**스트레스 테스트**:

1. **Tail 60 제한의 실제 역할**:
   - 기존: ESC 로 닫힌 stale modal 이 scrollback 에 남아있어도, tail 60 제한으로 "최근 라이브 modal" 만 포착.
   - 분석기: "envelope 구조" 만 보므로 tail 제한 없음 → **더 정확**.
   
   ✅ **설계 정당성 유지**: 실제로 상위호환.
   
   하지만 **"폐기" 가 아니라 "강화"** — 근거 재표현 필요.

2. **3단 구조의 "Yes 아래 divider 부재" 체크는 정말 필수인가?**
   - 기존: Telegram scrollback echo 를 필터링하기 위해 필수.
   - 분석기: VT 상태 머신은 "어느 row 가 현재 모달을 나타내는가" 를 알므로, divider 체크가 불필요할 수 있음.
   
   **실제 시나리오**: 사용자가 텔레그램에 모달 내용을 복사해 pane 에 echo → Claude Code 도 여전히 자기 input divider 를 렌더 (화면에는 보이지 않으므로) → parser 의 3단 체크가 이를 필터링.
   
   분석기는 **VT 상태 만으로는** "텔레그램 echo 된 historic modal" 과 "현재 라이브 modal" 을 구분할 수 없을 수 있음.
   
   📌 **필수 설계 변경**: 분석기의 region_tagger 가 "텔레그램 echo 감지" 규칙을 명시. 또는 case-02 (user-echo 판별, §4 gap) 를 우선 구현 후 검증.

---

## 2. Shadow Run ≠ Proof: 놓칠 수 있는 증상들

**설계 주장** (§5 Step 4-α):
> 72h 연속 diff 허용 범위 내 + 사용자 확인 → 통과.

**판정 기준** (§5):
- `block_commit` ±5%.
- `approval_show` ⊇ `is_approval=1` (superset OK).
- 스피너 rate ≤ 5건/초.

**놓칠 수 있는 증상들**:

### 2.1 모달 스택 오류 (Multi-modal stacking)

- **시나리오**: 신뢰 프롬프트 + 승인 박수 + 선택지 피커가 동시에 뜨는 엣지 케이스.
- **기존 parser**: 우선순위 분기로 처리 (core.py:438-462).
- **분석기**: envelope 기반 → 여러 modal 의 row 범위가 겹칠 때 어느 것을 선택? rule unspecified.
- **shadow run 에서 발견 불가**: 72h 동안 그런 엣지 케이스가 자연 발생할 확률 낮음 (case-01/02/03 처럼).

📌 **필수**: case-05 (multi-modal) synthetic fixture 추가.

### 2.2 VT 구현 ≠ tmux 렌더 (Row 어긋남)

- **시나리오**: tokenizer → VT 상태 머신이 tmux 실제 렌더와 off-by-1 으로 차이.
- **설계 대응** (§6): "shadow run 으로 실측 diff. 실패 시 row 탐색 범위를 ±12 로 확대".
- **문제**: row offset 이 ±12 범위면, region_tagger 의 "이 row 는 어떤 구역인가" 판정이 모호. envelope 구조 검사로 보정 가능하지만, 특정 row 에만 의존하는 로직 (예: "마지막 완료된 divider 아래") 은 위험.
- **shadow run 에서 발견 불가**: 오프라인 테스트(step-2-results) 에서는 synthetic ANSI 사용 → tmux render 차이 미검증.

📌 **필수**: Step 4-α 에서 "row offset variance" 를 명시적으로 추적. variance > ±5 면 실패.

### 2.3 사용자 입력(user-echo) 과 Claude 출력의 경계 오류

- **시나리오**: case-02 — 사용자가 텔레그램에서 입력 → bridge 가 send_input → pane 에 echo → Claude 가 그걸 문맥으로 이해.
- **기존 parser**: 마지막 ❯ 를 찾아 그 아래만 응답으로 간주.
- **분석기**: §4 gap "user_prompt 이벤트 미구현".
- **shadow run 에서 발견 불가**: parser 와 분석기를 **병행 실행**하면, parser 는 여전히 echo 를 제대로 필터링. 분석기의 부족이 "추가 이벤트" 로 나타나므로 diff 에서 보이지만, **실제 Telegram 에서 사용자 입력을 받았을 때 echo 처리를 제대로 하는가는 검증 불가**.

📌 **필수**: Step 4-α 에서 사용자 입력이 실제로 오면 (텔레그램 상호작용), shadow 분석기의 user_prompt 이벤트가 정확히 emit 되는지 수동 QA.

---

## 3. Rollback Env Flag 의 실제 작동 여부

**설계 주장** (§5 Step 4-γ):
> `BRIDGE_DISPATCH_SOURCE=parser|analyzer` 환경변수로 선택. 프로덕션 1주 관찰. regression 발생 시 env 하나로 즉시 롤백.

**재점검**:

### 3.1 Shared State (events.jsonl) 오염

**문제**: Step 4-α (shadow run) 동안 parser 와 분석기가 **같은 파일 `~/.claude-bridge/panes/<session>/events.jsonl`** 에 쓸 때:

- parser: `{..., "t": "flush_block", ...}` 이벤트 기록.
- 분석기: `{..., "t": "block_commit", ...}` 이벤트 기록.

Step 4-γ 에서 `BRIDGE_DISPATCH_SOURCE=parser` 로 롤백했을 때:

- 기존 events.jsonl 에는 이미 분석기 이벤트들이 섞여있음.
- 이후 "무회귀 2주" 판정 (§9 Step 5) 을 위해 events.jsonl 을 검사할 때, **이전 분석기 이벤트가 노이즈**가 됨.
- rollback 직후 events.jsonl 을 truncate/재시작하지 않으면 오염 영구.

**설계가 제시한 대응**: 없음. 설계 §5 "기존 events.jsonl 과 **이벤트 diff** 를 주기적 (1h) 계산" 에서 "이벤트 diff 의 스토리지" 를 명시하지 않음.

📌 **필수 설계 변경**: 
- shadow run 기간: `~/.claude-bridge/panes/<session>/analyzer-events.jsonl` (별도 파일).
- Step 4-γ cutover 시: rollback 경로 == 기존 parser events.jsonl 으로 되돌림 (truncate 또는 rotation).
- Step 5 "무회귀 2주": 어느 이벤트 스트림을 기준으로 하는가 명시 (parser / analyzer / 합산?).

### 3.2 Telegram Message ID / Edit Cursor 의존성

**문제**: Step 4-β (feature parity) 에서 분석기 기반 dispatcher 가 Telegram 메시지를 보낼 때:

- 분석기 이벤트 offset: 5000 (pipe-pane 파일의 byte offset).
- 해당 block_commit 을 Telegram 으로 전송 → message_id=12345 할당.
- step-4-γ 롤백 후 `BRIDGE_DISPATCH_SOURCE=parser` 로 변경:
  - 같은 offset 5000 이 다시 처리될 때, parser 는 다른 block 경계를 (tail 정책 차이로) 추출할 수 있음.
  - 그러면 기존 message_id=12345 는 **오아판** (orphaned).

**설계가 제시한 대응**: 없음. BridgeAdapter 의 상태 관리 (message_id cache, edit cursor) 가 §3 TO-BE 에서 언급되지 않음.

📌 **필수 설계 변경**:
- BridgeAdapter 의 상태 저장소: offset-based 가 아닌 **content-hash based message tracking** 또는, rollback 시 재전송 용인.
- 또는, Step 4-γ cutover 이후 "구 parser 기반 message_id" 를 명시적으로 폐기 (기존 메시지 delete + 새로 시작).

### 3.3 Parser 가 Step 4-γ 중에 진화하는 경우

**문제**: Step 4-γ (대략 1주) 동안, 기존 parser.py 가 frozen 이더라도 **의존성** (tmux.py, receiver.py 등) 이 진화할 수 있음.

- 예: receiver.py 에서 새 이벤트 타입 지원 → parser 도 그에 맞춘 수정 필요.
- 아니면 tmux 명령이 변경되어 capture-pane 의 출력 포맷이 달라짐 → parser 의 regex 조정 필요.

**설계가 제시한 대응** (§10 Q3):
> 권장: Step 4-γ 시작부터 freeze, Step 5 에서 제거.

→ 권장일 뿐 **강제 메커니즘 없음**. 개발자가 "parser.py 건드리지 말아야 한다" 를 기억해야 함.

📌 **필수 설계 변경**:
- frozen parser.py 를 `git assume-unchanged` 또는 pre-commit hook 으로 보호.
- 또는, Step 4-γ 시작 시 parser.py 를 별도 branch 로 snapshot → core.py 는 조건부로 어느 branch 를 사용할지 선택.

---

## 4. Step 4-β (Feature Parity) 의 검증 불충분

**설계 주장** (§4, §5 Step 4-β):
> §4 의 모든 이벤트 타입을 분석기에 추가 + fixture test.
> case-01/02/03 raw 재수집 — shadow run 기간에 자연 재현되지 않으면 재현 스크립트 작성.

**누락된 내용**:

### 4.1 "모든 이벤트" 의 정의가 불명확

**§4 gap table**에서:
- `approval_confirm` / `approval_deny` — "Enter 감지 + ❯ 최종 위치 관찰" (난이도 중).
- `user_prompt` — "send_input log 상관" (난이도 하).
- `compaction_start` — "compact 배너 regex 이관" (난이도 하).
- ... 외 5개.

**문제**: 
- 각 이벤트의 "정확한 schema" 가 미정의. `approval_confirm` 의 payload 는? `method`, `tool`, `summary` 등 어떤 필드?
- 테스트 케이스 ("fixture test") 가 어떤 수준인가? synthetic ANSI 인가, 실제 case-01/02/03 raw 인가?
- "fixture 채우기" 리소스: 각 이벤트 타입당 몇 줄의 코드 + 테스트?

📌 **필수 설계 변경**: Step 4-β 의 각 이벤트 타입을 **상세 spec 으로 정의**. 필드명, 필드 타입, 예시 JSON.

### 4.2 Case-01/02/03 "재현 스크립트" 의 현실성

**설계 주장**:
> case-01/02/03 raw 재수집 — shadow run 기간에 자연 재현되지 않으면 재현 스크립트 작성 (bridge 자체 리플레이 경로 권장, tmux replay 보다 안정).

**문제**:

- case-03 은 "500줄 eviction 요구" (step-3-design.md §10 Q1) — 자연 발생하려면 매우 긴 세션 필요.
- "bridge 자체 리플레이 경로" 는 구현되지 않았음 (현재 bridge 는 리플레이 기능 없음).
- tmux 의 `send-keys` 로 재현하면, Claude Code 의 **비결정성** (AI 응답 다름) 때문에 정확한 case-03 재현 불가능.

**설계의 대안** (§10 Q1 옵션):
- (B) 리플레이 스크립트 — "안정적이나 작성 비용 2-3일".
- (C) 합성 fixture — "빠르나 실제 Claude Code 출력과 괴리 가능".

→ **"작성 비용 2-3일" 이 견적인데, 누가 언제 할 것인가 명시 안 됨**. Step 4-β 에는 포함되어야 함.

📌 **필수 설계 변경**: case-01/02/03 수집을 Step 4-β 의 명시적 deliverable 로. 기한과 담당자 명기.

---

## 5. "프로덕션 2주 무회귀" 의 정의 불명확

**설계 주장** (§9 Step 5 exit criteria):
> - [ ] Step 4 이후 **프로덕션 2주** 무회귀.

**문제**:

- "무회귀" 의 증거는 무엇인가? 사용자 보고? events.jsonl diff?
- **침묵 실패** (silent failure): 분석기가 이벤트를 emit 하지 않았는데, Telegram 에 전달되지 않은 block 가 있다. 사용자는 "아무 것도 안 온다" 고 보고 → 하지만 events.jsonl 에는 기록도 없음.
- 이런 증상을 "무회귀" 판정에 포함시키려면, **능동적 모니터링** (대시보드, 알림) 이 필요한데, 설계에 없음.

📌 **필수 설계 변경**: 
- "무회귀" 판정 방법: 
  - (1) events.jsonl 에서 `bridge.suppress` (drop 된 이벤트) 개수 추적.
  - (2) `block_commit` 발생 대비 `bridge.dispatch` (실제 전송) 비율 모니터.
  - (3) 사용자 "응답 없음" 버그 리포트 0건.
- 목표치: events.jsonl 에서 `slip` / `suppress` 이벤트가 2주간 0건, 또는 ±2% 이내.

---

## 결론: 단일 underspecified 단계

**가장 위험한 구간**: **Step 4-γ cutover (adapter dispatch 전환)**.

이유:
1. **공유 상태 오염** (events.jsonl) — shadow run 이벤트가 섞임 → rollback 후 cleanup 미명시.
2. **message_id tracking loss** — Telegram 메시지 상태가 offset-based 경계와 불일치 → 오아판 메시지.
3. **분석기-parser 경계의 시간차 불일치** — 같은 offset 의 block 경계가 다름 → 중복 전송 또는 누락.
4. **frozen parser 강제 메커니즘 부재** — 권장만 있음, 구현 (pre-commit hook 등) 없음.

→ **Step 4-γ 는 "1주 관찰" 이 아니라 "72h 통제된 테스트" 를 권장**. 프로덕션 전환 기준을 재작성.

---

## 필수 설계 수정 체크리스트

- [ ] step-2-results 의 synthetic fixture 기반 case-01/02/03 raw ANSI 재수집 경로 명시 (Step 4-β).
- [ ] region_tagger 의 multi-boot (경계 C) + multi-modal (case-05) 판정 규칙 추가.
- [ ] shadow run 의 rotate 감지 / truncation 규칙 명시 (Step 4-α criteria 갱신).
- [ ] events.jsonl 스토리지: shadow run 이벤트와 기존 parser 이벤트의 분리 방식 (별도 파일 vs 필터).
- [ ] Step 4-γ rollback 시 events.jsonl cleanup / rotation 정책.
- [ ] Message ID tracking: offset-based 대신 content-hash 또는 rollback 시 재전송 용인.
- [ ] Frozen parser 강제 (pre-commit hook, git assume-unchanged).
- [ ] Case-01/02/03 수집 deadline / 담당자 명기.
- [ ] "무회귀 2주" 의 정량적 기준 (slip/suppress 개수, dispatch 비율, user report).
- [ ] Row offset variance 모니터링 (±5 기준).
