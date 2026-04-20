# Step 2 — 오프라인 분석기 상세 스펙

- 상태: **확정 초안** (코딩 착수 전 사용자 승인 필요)
- 작성일: 2026-04-19
- 상위 계획: [README.md](README.md)
- 전제 근거: [evaluation.md](evaluation.md), [baseline/case-0{1..4}/evaluation.md](baseline/README.md)

## 목적

baseline 4 케이스의 raw pipe-pane 로그를 입력으로 받아, bridge 가 소비 가능한
**이벤트 스트림** (JSON Lines) 을 결정적으로 산출하는 **오프라인 CLI** 를 만든다.
프로덕션 bridge 는 건드리지 않는다 (Step 4 이후).

통과 기준은 [README.md#decision-criteria-step-3-게이트](README.md#decision-criteria-step-3-게이트).
본 스펙이 확정되면 Step 2 체크리스트의 각 bullet 이 실제 구현 단위가 된다.

## 입력 / 출력

### 입력

- `raw_log_path: Path` — pipe-pane 생성 append-only 파일 (ANSI 포함).
- `events_log_path: Path | None` — 같은 세션의 `dump.event` JSONL. 상관 검증용 (optional).
- `rotation_hint: list[Path] | None` — rotation 된 이전 파일들 (연속 스트림 재조립).
- `send_input_log: list[(offset, bytes)] | None` — bridge 가 pane 에 송신한 입력
  기록. case-02 user-echo 구별에 사용 (optional).

### 출력

`stdout` 로 **JSON Lines**. 각 라인은 1개 이벤트:

```json
{"offset": 123456, "t": "block_commit", "kind": "response",
 "text": "⏺ 응, 커밋 0cc8aa7 완료됨.", "region": "content"}
```

필드:
- `offset: int` — raw 파일 내 byte offset (이벤트 확정된 token 의 **end** 위치).
- `t: str` — 이벤트 타입 (아래 스키마).
- `region: str` — `content` / `input_box` / `modal_overlay` / `chrome`.
- 타입별 payload (아래).

비결정적 필드 (wall-clock time 등) 는 넣지 않음 — fixture 테스트가 reproducible 해야.

## 파이프라인

```
raw bytes
  ↓  [1] ANSI tokenizer (streaming)
tokens
  ↓  [2] VT 가상 스크린 (cursor, cells, alt-screen, scroll region)
screen state + dirty lines
  ↓  [3] line-commit detector (cursor 이동 / CR-LF / 스크롤 시점)
committed lines (with region tag)
  ↓  [4] region tagger (box-drawing / position / send_input 상관)
tagged lines
  ↓  [5] event classifier (regex on committed + structural cues)
raw events
  ↓  [6] state machine (approval lifecycle / dedup)
final events → JSON Lines
```

각 단계는 **순수 함수 + 상태 머신** 조합. 뒤로 갈수록 상위 의미를 입힌다.

## [1] ANSI Tokenizer

`(offset_start, offset_end, kind, payload)` 토큰을 yield.

kind:
- `text` — UTF-8 decode 된 printable 문자열
- `csi` — `\x1b[` 시작, 명령 문자로 끝 (e.g. `\x1b[31m`, `\x1b[2J`, `\x1b[H`)
- `osc` — `\x1b]` ~ `\x07` 또는 `\x1b\\`
- `cr` / `lf` / `bs` / `ht` — C0 단일 바이트
- `alt_screen_on` / `alt_screen_off` — `\x1b[?1049h/l` 전용 토큰으로 승격 (모달 검출
  강화, case-01)

유니코드 box-drawing (`╭`, `╮`, `╰`, `╯`, `│`, `─`, `╌`) 은 `text` 로 유지하되 regex
검사 단계에서 특수 취급.

**테스트**: `tests/test_ansi_tokenizer.py` — 몇 가지 합성 입력 + baseline case-04
raw 의 처음 100 토큰 snapshot.

## [2] VT 가상 스크린

기본 사양: **24 행 × 200 열**. 필요 시 CSI `\x1b[8;H;Wt` 수신으로 resize.

반드시 구현:
- cursor (row, col) 이동: `\x1b[<n>A/B/C/D`, `\x1b[<r>;<c>H`, `\x1b[H`
- erase: `\x1b[2J` (all), `\x1b[K` (line from cursor)
- scroll region: `\x1b[<t>;<b>r` + `IND`/`RI`
- SGR: 색/굵기는 rendering 용으로 보관만 (attr matrix). 분석에서 사용 안 함.
- alt screen: `?1049h/l` — enter/leave 시 primary/alt 두 screen 상태 분리.

생략 가능 (PoC 범위):
- `tab stops`, `mouse tracking`, `character sets (SCS)`, `DEC modes` (alt screen 제외)
- `scrollback` — 우리는 file offset 이 곧 scrollback 이므로 VT 내부 scrollback 불필요.

**테스트**: `tests/test_vt_screen.py` — (a) alt-screen enter/leave 후 primary 복원
(b) `\x1b[H\x1b[2J` 로 clear 후 새 글씨가 (0,0) 에 찍히는지 (c) scroll region.

## [3] Line-commit 검출

"언제 한 line 이 **최종** 이 됐다고 판단할 것인가" 는 본 PoC 의 핵심 가설.

### 규칙 (후보, Step 2 구현 중 1개 선정)

- **R1 (cursor-leaves-and-stays)**: cursor 가 line L 에서 다른 line 으로 이동 후
  **K 토큰** (default K=64) 동안 L 로 돌아오지 않으면 L 의 현재 내용을 commit.
- **R2 (line-feed 기반)**: `\n` / `IND` / 스크롤 발생 시점에 직전 line commit.
- **R3 (cursor-above-bottom)**: cursor 가 bottom line 바깥으로 넘어가며 스크롤이
  트리거되면 버려지는 top line commit.

Claude Code UI 는 매 tick 부분 재렌더 (커서 이동 + in-place overwrite) 를 많이
쓰기 때문에 **R1 + R2 조합** 이 현실적. R3 은 append-only content 영역에서만.

**테스트**: case-04 keyframe 3 tick 에서 각각 몇 줄이 commit 돼야 하는지 hand-label
한 expected list 와 비교.

## [4] Region tagger

각 commit line 에 `region` 속성 부여.

- **modal_overlay**: 상하 horizontal divider (`─` 연속, 길이 ≥ 64) 사이의 line.
  alt-screen 진입 후의 line 도 modal.
- **input_box**: 마지막 non-chrome line 이 `❯ ` 시작 + cursor 가 해당 line. 아래
  divider 까지 input_box.
- **chrome**: `⏵⏵ bypass permissions`, `? for shortcuts` 등 bottom HUD.
- **content**: 그 외 전부 (본문 `⏺` 블록 영역).

**send_input 상관** (case-02): `send_input_log` 가 주어지면, 분석기는 최근 2초
이내 송신된 bytes 의 문자열 prefix 를 기억. input_box region 의 line 이 그 prefix
로 시작하면 `region_sub="user_echo"` 추가. `approval_show` 이벤트는 user_echo
region 에서 **발화 금지**.

## [5] 이벤트 스키마 (확정)

모든 이벤트 공통: `offset: int`, `t: str`, `region: str`.

### 5.1 `block_commit`
```
{t: "block_commit", kind: "response" | "tool_call",
 text: str, region: "content"}
```
`⏺ ` 로 시작하는 line + 후속 continuation line 의 concatenation. continuation
기준: 다음 `⏺` 또는 empty line 또는 region 전환까지.

### 5.2 `approval_show` / `approval_confirm` / `approval_deny` / `approval_cancel`
```
{t: "approval_show", tool_hint: "edit" | "write" | "bash" | ...,
 summary: str, region: "modal_overlay"}
{t: "approval_confirm", choice: 1 | 2, region: "modal_overlay"}
{t: "approval_deny", choice: 3, region: "modal_overlay"}
{t: "approval_cancel", method: "esc" | "alt_leave", region: "modal_overlay"}
```

**검출 (case-01, case-04 합)**:
- Primary (structural): modal_overlay region + `❯\s*1\.\s*Yes` + `3\.\s*(No|Skip)` +
  footer `Esc to cancel`.
- Secondary (wording): broad `"Do you want to"` / `"Allow .* to"`. primary 충족 시
  tool_hint 추출에만 사용.
- confirm/deny: approval_show 활성 중 `❯` 위치 변경 + Enter (`\r`) 감지. Enter 바로
  다음 token 에서 modal_overlay 소멸 → 선택된 row 를 choice 로.
- cancel: modal_overlay 가 Enter 없이 소멸 (ESC 는 CSI 로 안 오는 경우 많음 — alt
  screen leave 또는 `\x1b[2J` + cursor home 조합으로 간접 검출).

### 5.3 `busy_enter` / `busy_exit`
```
{t: "busy_enter", label: "Thinking" | "Reading" | ..., region: "content"}
{t: "busy_exit", region: "content"}
```
`✶ <label>…` 또는 `* <word>` 애니메이션 감지. Spinner rate gate (README Criteria 4)
는 diff event 를 초당 ≤ 5 로 coalesce.

### 5.4 `session_boot` / `session_resume`
```
{t: "session_boot", region: "content"}
{t: "session_resume", region: "content"}
```
배너 텍스트 (`Welcome to Claude Code` / `Resuming session`) 로 감지.

### 5.5 `user_prompt`
```
{t: "user_prompt", text: str, region: "input_box"}
```
send_input 상관으로 확정. 없으면 발화 안 함.

### 5.6 `compaction_start` / `limit` / `trust_prompt` / `resume_picker`
case 별 구체 signal 은 Step 2 구현 중 확정. 스키마 자리만 예약.

## [6] 상태 기계 규칙

**단일 불변식**: `approval_show` 이후 `approval_{confirm,deny,cancel}` 전까지는
`block_commit` 이벤트를 **emit 하지 않음** (내부 buffer 에 hold).

해소 시점에 buffer 를 flush (승인 종료 후 새 `⏺` 출현 전에 in-flight 였던 블록 순서
보존).

**오프셋 dedup**: `(offset, t)` 튜플의 first emit 만 유효. 재발화 시도는 즉시 drop
+ debug log.

**과거 재해석 금지** (case-01): 분석기 cursor 는 단조 증가. cursor 이하 파일 영역
은 다시 읽지 않는다.

## Dedup / 커서 관리

- analyzer state: `{file_offset: int, screen: VTScreen, fsm: StateMachine}`.
- file rotation: `file_offset` 는 **(logical) session offset** — 물리 파일 경계가
  아니라 pipe-pane 이 기동된 시점 기준 누적 byte.
- `rotation_hint` 가 주어지면 이전 파일들 전체 → 신 파일 append 순서로 읽되, 커서는
  session offset 그대로 이어짐.
- checkpoint: 매 1000 이벤트마다 `{session_offset, screen_hash, fsm_state}` 를
  stderr 에 찍어 재현성 검증.

## CLI 인터페이스

```
python -m tools.pipe_pane_analyzer \
    --raw /path/to/raw-20260419_172304.log \
    [--rotate-before path1 path2 ...] \
    [--events /path/to/dump/*/pane_tick.jsonl] \
    [--send-input /path/to/send_input.jsonl] \
    [--until-offset N] \
    > events.jsonl
```

- `--until-offset N`: 재현 디버깅용, N 도달 시 분석 중단.
- exit code: 0 (정상), 2 (파싱 실패 — 마지막 처리 offset 을 stderr 에 dump).

## Fixture 및 테스트

`tools/fixtures/baseline/` 은 `docs/plans/20260419-pipe-pane-redesign-poc/baseline/`
의 raw 파일을 심볼릭 링크.

`tests/test_pipe_pane_analyzer.py`:
- `test_case_01_esc_cancels_approval()` — baseline case-01 raw 에서
  `approval_show` 1회 + `approval_cancel(method="esc")` 1회 + 이후 block_commit
  정상 재개.
- `test_case_02_user_echo_not_approval()` — case-02 raw 에서 user_echo region 의
  "Do you want to" 가 `approval_show` 로 승격되지 않음.
- `test_case_03_long_session_no_eviction()` — case-03 raw 의 끝까지 분석 후
  event count ≥ baseline BUG_REPORT 의 실제 block 수.
- `test_case_04_edit_approval_detected()` — case-04 raw 에서
  `approval_show(tool_hint="edit")` 1회 발화, 이후 `queue_slip` 상응 loop event 0건.

## 통과 기준 (Step 3 Gate 투입 자료)

Step 2 구현 종료 시 다음을 본 스펙 옆에 `step-2-results.md` 로 리포트:

1. 각 fixture 테스트 pass/fail + event JSONL snapshot.
2. LoC 비교: `tools/pipe_pane_analyzer.py` (+ VT 모듈) **vs** `bridge/parser.py
   line 214-332` + `bridge/stream_queue.py`. README Criteria 3 (≤ 70%) 판정.
3. Spinner rate 실측 (README Criteria 4).
4. 실패 케이스 재현 가능성 (README Criteria 5): `--until-offset` 로 하나 sample.

## 범위 밖 (Out of scope)

- 프로덕션 bridge 통합 (Step 4)
- TUI 다이얼로그 외의 Claude Code 기능 (compaction / limit 등 이벤트 **스키마만**
  예약, classifier 구현은 Step 2 필수 아님 — case-01~04 에 등장하지 않음).
- 성능 최적화 — 오프라인 도구이므로 baseline 4 케이스 raw (최대 ~1MB) 를 수 초
  이내 처리하면 충분.

## 오픈 이슈 (구현 중 확정)

- **O1**: Line-commit 규칙 R1/R2/R3 중 실제 조합 — fixture 로 결정.
- **O2**: Claude Code 가 `❯` 화살표를 어떻게 그리는지 (reverse video vs text) —
  raw 확인 후 confirm 감지 규칙 고정.
- **O3**: ESC 처리. pipe-pane 에 실제 `\x1b` byte 가 찍히는지 vs UI 가 alt-screen
  leave 로만 반응하는지 — case-01 raw 로 확정.
- **O4**: Coalescing 윈도 크기 (busy_enter/exit 간 spinner diff 흡수) — 500ms
  초안, Criteria 4 달성 기준으로 조정.

## 진행 순서 (구현 권고)

1. `tools/pipe_pane_analyzer.py` skeleton + CLI 인자 + JSON Lines 출력.
2. [1] ANSI tokenizer + 단위 테스트.
3. [2] VT 가상 스크린 (최소 세트: cursor, erase, alt-screen) + 단위 테스트.
4. [3] line-commit 검출 — case-04 keyframe 3 tick 으로 규칙 선정.
5. [4] region tagger — box-drawing 기반 modal_overlay 부터.
6. [5] block_commit + approval_* 이벤트 — case-04 fixture green.
7. [6] 상태 기계 + dedup — case-01 (ESC) 로 검증.
8. 나머지 이벤트 (busy/session_boot/user_prompt) — case-03 long session 으로.
9. LoC 측정 + step-2-results.md 작성 → Step 3 게이트 투입.
