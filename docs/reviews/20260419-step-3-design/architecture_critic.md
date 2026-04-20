# Architecture Critic Review: Step 3 Design

## 주요 우려사항 (심각도 순)

### 1. **LineCommit 의 `screen: tuple[str, ...]` 스냅샷 — 추상화 누수 (HIGH)**

**문제**: LineCommit (line_commit.py:28-34) 가 `screen: tuple[str, ...] = ()` 필드를 포함해 VTScreen 의 전체 그리드를 하위 모듈로 전달한다.

```python
@dataclass
class LineCommit:
    offset: int
    row: int
    text: str
    reason: str
    in_alt: bool = False
    screen: tuple[str, ...] = ()  # ← 누수
```

**왜 비판인가**:
1. **책임 분리 위반**: LineCommitter 는 "row 확정" 만 담당해야 하는데, VTScreen 전체 상태를 알고 있어야 한다. 다음 계층(RegionTagger)이 line_commit.py:68 `screen=tuple(self.vt.snapshot())` 으로 스냅샷을 채워야 한다는 것은 **설계 의도가 불명확**함을 의미.

2. **RegionTagger 의 암묵적 의존**: region_tagger.py 가 TaggedLine 을 만들 때 `commit.screen` 에 접근할 가능성이 높다 (modal_overlay 판정 시 grid row 범위 검사 등). 코드에 명시되지 않았지만 **암묵적 계약**.

3. **성능 누수**: 매 commit 마다 24×200 그리드 전체를 복사해 tuple 로 변환. case-04 에서 452 개 commit × 4800 cell = 216만 cell 복사. 설계 문서에서 "perf: chunk 당 tokenize+VT 비용" 을 리스크(§6) 로 나열했으나 **screen snapshot 비용은 언급 없음**.

**구체적 권고**:
- **Option A (권장)**: `screen` 필드를 제거. RegionTagger 가 자신의 VTScreen 상태를 유지하면서 commit 의 row/in_alt/offset 만 참조. LineCommitter 는 오직 "cursor 동작" 만 감시.
- **Option B**: LineCommit 을 2단계 split — `LineCommit` (row/offset/text/reason) + 별도 `ScreenSnapshot(commits: list[LineCommit], screen: tuple)` wrapper. 그룹 단위로 스냅샷 전달.
- **Option C (최소 변경)**: `screen` 을 Optional 로 marked, docstring 에 "RegionTagger 내부 사용만, bridge 소비 금지" 명시. 테스트에서 screen == () 인 경우 커버.

**현재 위험도**: moderate → medium (실제 코드를 보지 않았으므로 정확한 누설 정도 불명)

---

### 2. **BridgeAdapter 의 경계가 모호함 — "실제 아키텍처 경계인가?" (MEDIUM)**

**문제**: step-3-design §3 그림에서 BridgeAdapter 를 신규 경계로 소개했지만, 정의가 불충분하다.

```
AnalyzerEvent → [BridgeAdapter]
                · Telegram dispatch
                · dump.events() 로깅
                · send_input 상관
```

**왜 비판인가**:
1. **책임 범위 불명확**: "Telegram dispatch" 는 기존 `sender.py` 가 하는 일인데, adapter 가 sender 를 호출하는가? 아니면 자신이 메시지를 구성하고 sender 에 위임하는가?

2. **send_input 상관의 위치**: case-02 (user-echo 구분) 이 "send_input log 상관으로 구조 해결" (§4) 이라고 했는데, 이걸 누가 구현하나?
   - **분석기 내부** (event_classifier.py)? → region_tagger.py:129-132 "send_input_log 가 주어지면" 으로 암시
   - **BridgeAdapter**? → step-3-design §4 table row "case-02 user-echo 구분" 에서 "adapter 에서 user_prompt 이벤트를 우선 발화, 이후 동일 텍스트가 content 로 오면 suppress" (§6) 로 서술

   두 곳 모두에서 언급되어 **책임이 분산**.

3. **state 관리**: adapter 가 Telegram 전송 기록을 어떻게 유지하나? 기존 bridge.py 의 approval/trust/resume picker 상태 변수들을 adapter 로 옮기나? 아니면 별도 adapter state object?

**구체적 권고**:
- **BridgeAdapter 명세서 작성**: `bridge/adapter.py` (권장안 Option A, step-3-design §10.4) 의 공개 인터페이스를 **명시적 spec** 으로 문서화.
  ```python
  class BridgeAdapter:
      def __init__(self, telegram_sender: ..., dump_writer: ...):
          ...
      def process_event(self, event: AnalyzerEvent) -> None:
          """분석기 이벤트를 소비해 Telegram/dump 에 반영."""
          ...
      def handle_send_input(self, offset: int, text: bytes) -> None:
          """send_input log 상관점, user_prompt 이벤트 생성."""
          ...
  ```
- **상태 관리 선언**: adapter 가 소유할 상태 필드 명시 (e.g., `last_approval_offset`, `pending_user_input_segments`).
- **send_input 상관 책임 명확화**: 
  - 분석기는 region_tagger 에서 `region_sub="user_echo"` 추가만 → EventClassifier 에서 `approval_show` 를 `region="user_echo"` 에서 emit 금지
  - Adapter 는 `user_prompt` 이벤트를 **자신이 생성** (send_input log 읽으면서) → classify 가 아니라 source

---

### 3. **RegionTagger 의 상태 초기화 & 모달 감지 경계 (MEDIUM)**

**문제**: region_tagger.py 의 설계가 부분적으로 노출됨.

§4 step-2-spec 에서:

> modal_overlay: 상하 horizontal divider (`─` 연속, 길이 ≥ 64) 사이의 line.
> alt-screen 진입 후의 line 도 modal.

**왜 비판인가**:
1. **divider 초기 상태**: RegionTagger 가 처음 시작할 때 "이전 divider" 를 어떻게 초기화하나? 
   - 세션 시작 → ❯ input 박스 있음 (divider 있음) → approval 팝업 → divider 위아래 모두 있음
   - 따라서 첫 commit 부터 "modal 안에 있나?" 를 판단해야 하는데, 초기 상태가 불명.
   
   코드 레벨에서 확인 필요: `tools/region_tagger.py` 의 `__init__` 과 초기 `_last_divider_idx` 값.

2. **nested dividers 미처리**: Claude Code 가 이론적으로 여러 개 divider 를 한 화면에 그릴 수 있나? (예: 내부 도구 출력 + 사용자 입력 박스 divider 중첩)
   - step-2-spec §4 문서는 "상하 divider 사이" 로 단순화했으나, 실제 case-01/02/03 raw 로그에서 이런 경계 케이스가 있을 수 있음.
   - shadow run (§5 step 4-α) 에서 "실측 diff" 를 보면 드러나겠지만, 설계 단계에서 **명시적 예외 처리** 선언 필요.

3. **alt-screen 과 divider 의 우선순위**: "alt-screen 진입 후의 line 도 modal" 인데, alt 화면 내에도 divider 가 있으면 어떻게 하나?
   - 예: `?1049h` enter → divider 그음 → 그 사이 line → 다른 divider → exit `?1049l`
   - in_alt flag 가 우선인지, divider 위치 탐색이 우선인지 명확히.

**구체적 권고**:
- RegionTagger 명세 추가 섹션: "초기 상태 & edge cases"
  ```markdown
  ### 초기화
  - 세션 시작: divider 미발견 상태에서 시작 → 첫 commit 들은 content/chrome 로 tag
  - divider 감지 후 modal 영역 추적 시작
  
  ### Edge cases
  1. nested dividers (multiple 박스) → 가장 최근 divider 쌍만 추적
  2. alt-screen + divider 중첩 → alt-screen 우선 (in_alt=True → modal_overlay)
  3. divider 없이 alt-screen 진입 → 전체 alt 콘텐츠 modal (모달 도구: less, fzf 등)
  ```
- 테스트: `tests/test_region_tagger.py` 에 각 edge case fixture 추가

---

### 4. **VT 구현 & tmux 렌더 괴리 위험 (MEDIUM)**

**문제**: step-3-design §6 "리스크와 완화" 에서 명시했지만 해결 전략이 약함.

> VT 구현 ≠ tmux 렌더 (row 어긋남) | region_tagger 가 envelope 을 놓침| 
> shadow run 으로 실측 diff. 실패 시 row 탐색 범위를 ±12 로 확대하거나 col 패턴 보강

**왜 비판인가**:
1. **"row 어긋남" 의 원인이 불명확**: 
   - VTScreen 이 24×200 기본이지만, tmux pane 이 다른 크기일 수 있음. 설계에서 "resize 이벤트 추적" 은 step-2-spec [2] 에만 쓰여있고, 구현에서 실제 `CSI 8 ; H ; W t` 처리가 있나?
   - box-drawing 위치가 VT row N 이라고 생각했는데, 실제 pane 에서는 N±2 에 렌더되나?

2. **"row 탐색 범위 ±12 확대" 는 band-aid**:
   - RegionTagger 가 modal 경계를 찾을 때 divider 를 `region_tagger.py:37 _is_divider(text, min_len=32)` 로 strict 하게 찾고 있음. 
   - 만약 VT 와 tmux 행 수가 어긋나면 이 탐색이 **완전히 실패** (divider 를 못 찾으면 modal region 이 전혀 태깅 안 됨).
   - 사후 ±12 확대는 이미 miss 된 이벤트를 복구 못 함.

3. **alt-screen 의 다른 크기**: alt-screen 도구 (fzf, less) 는 특정 크기를 assume 할 수 있고, 화면이 이를 맞추지 못하면 (예: 터미널 resize 중) 렌더 버그 발생 가능.

**구체적 권고**:
- **VTScreen 크기 동기화**: pipe-pane raw 에서 실제 Claude Code 가 보낸 resize 이벤트 (`CSI 8 ; ... t`) 를 수집해 VTScreen 을 동적 resize.
  - baseline case-04 raw 분석: resize 이벤트가 몇 건 있나? 
  - 없으면 "고정 24×200 assumption 이 valid" 로 문서화.

- **RegionTagger 의 divider 놓침 감지**:
  - commit stream 에서 approval_show 를 **expected 했는데 emit 못 했을 경우** 경고 로그.
  - 예: "Do you want to" 가 있는데 divider 가 양쪽 다 없음 → fallback 으로 content region 으로 tagging 했음을 기록.

- **Shadow run 평가 기준 명확화** (§5 step 4-α):
  - 단순 "이벤트 diff" 가 아니라, **region 분류 diff** 를 추가 추적.
  - approval_show 가 "content" 로 오분류됐으면 critical, 그 외는 low priority.

---

### 5. **Event Classifier 의 상태 기계 단순성 의문 (LOW-MEDIUM)**

**문제**: step-2-spec [6] 상태 기계 규칙이 2줄이다.

> 단일 불변식: `approval_show` 이후 `approval_{confirm,deny,cancel}` 전까지는 `block_commit` 이벤트를 **emit 하지 않음**.

**왜 비판인가**:
1. **교집합 케이스**: approval 모달 진입 중에 block 이 **실제로 진행** 되는 경우 (예: 사용자가 승인을 지연하는 동안 background 에서 블록이 완료됨).
   - 현재 규칙: buffer 에 hold → approval 종료 후 flush
   - but what if 모달 종료 후에도 블록이 **계속 안 옴**? pending buffer 가 영원히 남나?

2. **FSM reset 메커니즘**: approval_cancel 또는 approval_confirm 후에 state 를 reset 한다고 했는데, 그 사이에 다른 approval 이 또 나타나면? (예: 첫 승인은 ESC, 두 번째는 Yes)
   - 상태 기계 다이어그램이 없어 circular/nested 케이스를 판단 불가.

3. **다른 모달들의 상태**: trust_prompt, resume_picker, limit 등도 각각 독자적 상태 기계를 가져야 하나? 아니면 approval 과 비슷하게 "나타났다가 소멸" 만 추적?
   - step-2-spec [6] 은 approval_show/cancel 만 다루고, 다른 이벤트는 명시 안 함.

**구체적 권고**:
- **EventClassifier 상태 기계 FSM 다이어그램 추가**: 
  ```
  START → (block_commit 가능) 
       → approval_show (buffer on)
       → (block_commit buffer)
       → approval_{confirm,deny,cancel} (buffer flush)
       → (block_commit 재개)
  ```
  - Reset 조건, timeout, nested 모달 처리 명시.

- **buffer overflow safeguard**: approval 이 N 분 이상 지속되면 버퍼 강제 flush (또는 경고 log).

- **다른 모달도 유사하게**: trust_prompt, resume_picker 역시 "진입-종료" 추적이 필요하면 동일 패턴 적용.

---

### 6. **send_input 상관의 시간 창 불명확 (LOW-MEDIUM)**

**문제**: step-2-spec §4 "send_input 상관":

> `send_input_log` 가 주어지면, 분석기는 최근 2초 이내 송신된 bytes 의 문자열 prefix 를 기억.

**왜 비판인가**:
1. **"최근 2초"의 의미**: session offset 기준인가? wall-clock time 기준인가?
   - offset 기준이면: 450KB 파일의 offset 변위로 "2초" 를 어떻게 정의? (네트워크 지연, tmux pipe-pane 배치 시간 등)
   - wall-clock 기준이면: pipe-pane raw 에 timestamp 가 없어 재현 불가.

2. **중복 입력**: 사용자가 같은 문자를 여러 번 send_input 했으면 (예: "yes" 를 3번)?
   - 가장 최근 것만? 모두 누적?

3. **타이밍 슬립**: approval 모달이 나타난 후, 사용자가 입력 (Telegram), send_input log 기록, 그리고 pane 에 반영될 때까지 약간의 지연 있을 텐데, 이걸 어떻게 맞추나?

**구체적 권고**:
- send_input_log 스키마 명시: `[(offset: int, text: bytes)]` 로 offset 기반 추적.
- "최근 2초" → "가장 최근 send_input 이후 `K` 개 token" (K 는 튜닝 상수) 로 변경.
- case-02 fixture 에서 실제 시간 차이 측정: approval_show offset vs 그 직전 send_input offset 사이 거리 기록.

---

## 종합 평가 & 의견

### 현재 설계의 아키텍처 건전성

**긍정**: 단일 책임, 관측성, 마이그레이션 안전성 3개 축에서 기존 대비 명확 우위.

**우려**: 
- LineCommit 스냅샷 누수 (HIGH) → 즉시 개선 필요
- BridgeAdapter 경계 모호 (MEDIUM) → Step 4 착수 전 명세 확정
- 모달 감지 초기 상태 (MEDIUM) → 테스트 커버로 검증

### Step 3 Gate 통과 권고

**조건부 통과 가능**, 단:
1. LineCommit 스냅샷 누수 제거 또는 명확한 계약 문서화
2. BridgeAdapter public interface spec 작성 (§10.4 옵션 A 추진)
3. RegionTagger 초기 상태 & edge case 명시
4. Shadow run 에서 region 분류 diff 추가 추적

현재 설계의 paradigm shift (snapshot → stream) 는 건전하며, 위 4개 항목은 **구현/검증 시점에 해결 가능**한 수준. PoC LoC (1423) 이 높아 보이지만, 기반 능력 (ANSI+VT) 추가를 감안하면 정당화됨 (§3 criterion 3a-3c pass).

**최종 의견**: Step 3 gate 통과, Step 4 진행 권장. 다만 상기 4개 항목을 Step 4-α (shadow run) 이전에 문서화/코드 정리 완료하면 온라인 통합 시 예상 밖 coupling 을 크게 줄일 수 있다.
