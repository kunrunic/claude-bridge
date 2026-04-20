# fixes — 스텝별 변경 지점

본 문서는 **Step 1, 2** 의 구체 변경 지점만 정의한다. Step 4~6 의 온라인 통합/제거는 Step 3 게이트 통과 후 별도 plan 폴더에서 상세화한다.

## Step 0 — 기준선 아카이브 ✅ 완료 (2026-04-19)

실제 구현: [baseline/README.md](baseline/README.md).

```
baseline/
├── README.md                             # 선정 근거 + 미선정 리포트 링크
├── case-01-esc-flush-block/              # bugreport/20260418_094744 — 경계 탐색
│   ├── SUMMARY.md / BUG_REPORT.md / tmux_capture.txt
├── case-02-scrollback-echo/              # bugreport/20260417_164104 — echo 오탐
└── case-03-streamqueue-eviction/         # bugreport/20260417_095801 — 500줄 창 유실
```

각 케이스의 `SUMMARY.md` 에 현상 한 줄 + 현 파서 실패 `file:line` + 재설계 요구사항.
`logs/` 는 크기 이유로 복사하지 않고 원본 `bugreport/<ts>/logs/` 경로만 참조.

## Step 1 — pipe-pane 생로그 수집 ✅ 코드 적용 완료 (2026-04-19)

> **실구현 반영**: 아래 내용은 실제 커밋 적용 후 문서로 동기화한 버전. 초안 대비 delta 는 각 소절 끝 **delta** 박스 참조.

### 1.1 `bridge/tmux.py` — pipe-pane 헬퍼

추가된 함수 (기존 `pane_output_async` / `send_input` / `send_key` 는 그대로):

```python
async def start_pipe_pane(log_path: str, target: str | None = None) -> bool:
    """raw ANSI 스트림을 파일에 append.
    옵션 없이 호출해 기존 pipe 가 있어도 새 command 로 교체된다 (man tmux).
    실패 시 False 반환 — monitor loop 는 계속."""

async def stop_pipe_pane(target: str | None = None) -> None:
    """command 인자 없이 pipe-pane 호출 → 기존 pipe 해제."""
```

- target-pane 은 기존 `_tp(target)` 헬퍼 사용 (`=<name>:` exact-match).
- 로그 경로는 `shlex.quote` 로 감싸 공백/따옴표 안전.
- 두 함수 모두 `try/except` 로 tmux 오류/타임아웃을 swallow → loop 지장 없음.

> **delta**: 초안의 `-o` 플래그 제거. `pipe-pane -o` 는 "기존 파이프 없을 때만 열기" (토글 용도) 의미라 우리 용도와 반대. 옵션 없이 호출하는 게 정답.

### 1.2 `bridge/config.py` — 설정 추가

```python
BRIDGE_PIPE_PANE_ENABLED: bool = os.getenv("BRIDGE_PIPE_PANE", "1") != "0"
BRIDGE_PIPE_PANE_DIR: Path     = Path.home() / ".claude-bridge" / "panes"
BRIDGE_PIPE_PANE_MAX_BYTES: int = 20 * 1024 * 1024   # 20MB → rotate
BRIDGE_PIPE_PANE_ROTATE_CHECK_SEC: float = 30.0      # 크기 체크 최소 간격
```

`os` 표준 라이브러리 import 도 함께 추가 (상단). `env_bool` 같은 사내 헬퍼는 존재하지 않아 `os.getenv(...) != "0"` 형태로 직접 처리.

> **delta**: `ROTATE_CHECK_SEC` 1개 추가. 초안은 "monitor tick 마지막에 크기 체크" 였으나 매 tick stat() 호출은 낭비 — 30초 minimum interval 도입.

### 1.3 `bridge/core.py` — 기동/종료/rotate 훅

**추가된 필드** (`Bridge.__init__`):
```python
self._pipe_log_path: Path | None = None
self._pipe_last_size_check: float = 0.0
```

**추가된 메서드**:
- `_new_pipe_log_path()` — `~/.claude-bridge/panes/<tmux>/raw-<YYYYMMDD_HHMMSS>.log` 생성. 디렉토리 자동 mkdir.
- `_pipe_attach()` — `monitor()` 진입부에서 호출. tmux.start_pipe_pane 성공 시 경로 보관.
- `_pipe_detach()` — `monitor()` 의 `finally` 블록에서 호출. 정상 종료와 `CancelledError` 경로 모두 커버.
- `_pipe_maybe_rotate()` — 매 tick 진입부에서 호출. 마지막 체크 후 30s 미만이면 skip, 20MB 초과 시 새 파일로 스왑.

**훅 위치**:
1. `monitor()` entry — `dump.start_tick(...)` 직후 `await self._pipe_attach()`.
2. `monitor()` `finally` — `await self._pipe_detach()` (`CancelledError` 및 정상 종료 모두).
3. 매 tick 루프 진입부 — `await self._pipe_maybe_rotate()` (has-session 체크 바로 앞).

**rotation 동작**: tmux `pipe-pane -t =<tmux>: 'cat >> <new_path>'` 은 기존 pipe 가 있어도 자동 교체. 따라서 `stop → start` 순서 불필요, `start_pipe_pane(new_path)` 한 번 호출로 atomic swap.

> **delta**: 초안의 "stop → start 순서 필요" 가정은 tmux 실제 동작과 다름 → 단일 호출로 교체.

### 1.4 `bridge/dump.py` — 변경 없음

초안에서 "raw_log_path kwarg 추가" 를 계획했으나, 기존 `dump.event(source, kind, **fields)` 시그니처가 임의 kwarg 를 이미 받음. 호출측에서 `raw_log_path=str(self._pipe_log_path) if self._pipe_log_path else None` 을 넘기면 끝.

**실제 동봉 지점**: `_flush_completed` 의 `flush_peek` 이벤트 1곳에 추가. `flush_block` / `queue_slip` / `flush_send_fail` 은 Step 2 분석기가 `flush_peek` 와 timestamp 로 조인 가능하므로 중복 기록 안 함.

> **delta**: dump.py 파일 자체 수정 없음. 호출측 1줄 변경만.

### 1.5 `bridge/session.py` — 변경 없음

초안의 "Session 객체에 `pipe_log_path` 필드" 는 `session.py` 가 **세션 탐색 유틸** 모듈이라 적용 대상이 없음 (`Session` 클래스가 존재하지 않음). 대신 `Bridge` 인스턴스 (`core.py`) 에 필드 배치 → 1.3 의 `_pipe_log_path` 로 해결.

> **delta**: session.py 파일 자체 수정 없음.

### 1.6 테스트

**신규** `tests/test_pipe_pane_tmux.py` — 8개 테스트:

| # | 검증 |
|---|------|
| 1 | `pipe-pane -t =<tmux>: 'cat >> <path>'` 인자 구성 |
| 2 | 공백/작은따옴표 있는 경로가 `shlex.quote` 로 안전 처리 |
| 3 | `target="other"` 전달 시 `=other:` 사용 |
| 4 | `BRIDGE_PIPE_PANE_ENABLED=False` 시 tmux 호출 0회 (no-op) |
| 5 | tmux returncode != 0 시 `False` 반환 (loop 계속) |
| 6 | tmux 예외 시 swallow + `False` |
| 7 | `stop_pipe_pane` 이 command 인자 없이 호출 (길이 3) |
| 8 | `stop_pipe_pane` 예외 swallow |

**주의 — 테스트 간 모듈 누수**:
bridge 패키지를 import 하기 위해 `sys.modules` 에 `telegram*` stub 을 심는데, **`sys.modules.pop("bridge", None)` / `importlib.invalidate_caches()` 를 호출하면 안 된다**. pytest 알파벳 순 collection 에서 `test_pipe_pane_tmux.py` 이후에 로드되는 `test_receiver_logging` / `test_scenarios` / `test_tmux_death` 의 `_load_bot()` 이 실패한다. 기존 테스트들과 동일하게 stub 만 한 번 심고 bridge 는 재사용하도록 둘 것.

**회귀 확인 결과**:
- 전체: `python -m pytest tests/ -q` → **186 passed** (신규 8 포함).
- 기존 178 테스트 모두 green — 내 변경이 추가한 regression 0건.

## Step 2 — 오프라인 분석기

### 2.1 `tools/pipe_pane_analyzer.py` — CLI

```
python tools/pipe_pane_analyzer.py \
    --input path/to/raw-YYYYMMDD.log \
    --out events.jsonl \
    [--fixture CASE_ID]
```

**내부 단계**:

1. **ANSI tokenizer** — 바이트 스트림 → token list. 구분 토큰:
   - `TEXT(chars)`
   - `CSI(params, final)` — 특히 `CUP`(커서 이동), `ED`/`EL`(지우기), `SGR`(색)
   - `OSC(...)` — title/hyperlink 등 (무시 대상)
   - `CR`, `LF`

2. **가상 스크린 버퍼** — 간이 terminal emulator. 폭은 pane width 가정치(예: 180) 로 초기화. 커서 위치와 각 셀 내용을 메모리에 유지. ANSI 이벤트 적용.

3. **line-commit 판정** — "어떤 줄이 확정됐는가" 규칙:
   - 커서가 해당 줄 아래로 이동한 뒤 `COMMIT_GRACE` tick 동안 되돌아오지 않으면 commit.
   - 혹은 `ED` 로 지워지지 않고 스크롤 out 된 경우 commit.
   - 확정된 줄만 event dispatch 대상.

4. **이벤트 디스패처** — 확정된 줄에 대해 prefix/pattern 매치:
   - `⏺` → `block_start` (또는 연속이면 `block_line`)
   - `❯<whitespace><text>` → `user_prompt`
   - `* <word>` 패턴 → `busy_spinner` (같은 word 연속이면 이벤트 미발행, word 변경 시만)
   - `Do you want to proceed` + 아래 `❯ 1. Yes` → `approval_show` (현재 screen 기준)
   - approval_show 상태에서 화면이 `❯ ` 입력 대기로 돌아가면 `approval_hide`
   - `Compacting conversation` / `Context limit` / `Quick safety check` → 개별 이벤트

5. **출력** — JSON Lines. 각 레코드:
   ```json
   {"ts": 1.234, "event": "block_start", "text": "⏺ Bash(...)"}
   ```

### 2.2 fixture 기반 회귀 테스트

`tests/test_pipe_pane_analyzer.py`:
- Step 0 에서 수집한 3건을 입력으로 돌려 기대 이벤트 시퀀스 정답지와 비교.
- 정답지는 `tools/fixtures/<case>/expected_events.jsonl` 로 수작업 작성 (plan 작성자가 직접 검수한 것).

### 2.3 평가 스크립트 (Step 3 입력)

`tools/eval_pipe_pane.py`:
- 각 case 에 대해 현 파서와 신규 분석기의 결과를 diff 표로 출력.
- Decision Criteria 5항목을 pass/fail 로 자동 체크 (LoC 카운트, 초당 스피너 이벤트 수 등).
- 결과 markdown 을 `docs/plans/20260419-pipe-pane-redesign-poc/evaluation.md` 로 저장.

## Step 3~6 — 게이트 이후

Step 3 결정 게이트 결과가 정해진 뒤 별도 plan 폴더에서 상세화. 현 시점에는 **전망선**만 기록:

- **Step 4**: `bridge/core.py` monitor tick 의 `④ 특수 상태 분기` 를 "이벤트 소비자" 로 치환. `StreamQueue.idx/last_fp` 제거 후보.
- **Step 5**: `parser.py:214-332` (`_response_region` + `extract_response_blocks` + `_is_block_active`) 삭제. 대신 `bridge/pane_events.py` (신규) 가 분석기 출력을 구독.
- **Step 6**: `docs/design/02`, `03` 을 TO-BE 로 재작성. AS-IS 는 `docs/design/archive/` 로 이동.

## 변경 금지 사항 (모든 스텝 공통)

- tmux target-session/pane 분리 규칙(`eecbbce`) 건드리지 말 것.
- `_t()` 콜론 접미 규칙(`908cbfd`) 유지.
- pipe-pane 실패가 기존 monitor loop 를 block 하지 못하게 할 것.
- 로그 파일에 **사용자 입력이 그대로 남는다** — `.gitignore` 에 `~/.claude-bridge/panes/` 가 포함되는지 확인 (로컬 경로라 무관하지만, bugreport 로 제출 시 scrub 필요).
