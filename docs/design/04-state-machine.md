# 04. 상태 머신

`Bridge` 인스턴스에는 10+ 개의 플래그가 있고, 각각 set/clear 경로가 흩어져 있다. 이 문서는 플래그별로 **누가 set, 누가 clear, monitor 가 어떻게 분기**하는지를 표로 고정한다.

## `Bridge` 인스턴스 필드 전수

`bridge/core.py:42-59`:

```python
class Bridge:
    def __init__(self):
        self.chat_id: int | None = None
        self.running: bool = False
        self.task: asyncio.Task | None = None
        self.last_hash: str = ""
        self.awaiting_approval: bool = False
        self.skip_permissions: bool = False
        self.current_session_id: str | None = None
        self.last_approval_summary: str = ""
        self.last_approval_context: str = ""
        self.last_approval_full: str = ""
        self.queue: StreamQueue = StreamQueue()
        self.trust_ack_pending: bool = False
        self.resume_picker_ack_pending: bool = False
        self.boot_notified: bool = False
        self.limit_reported: bool = False
        self._state_lock = asyncio.Lock()
```

추가로 `monitor()` 안에서 인스턴스에 늦게 set 되는 런타임 필드(`core.py:281-291`):

```python
self.dead_reported
self.was_busy
self.status_msg_id
self.busy_started_at
self.last_status_label
self.saw_compaction
self._pre_busy_had_compact
self.busy_pane_hash
self.busy_last_change_at
self.auto_compacting
self.compact_error_halted
self.busy_last_stream_at
```

## 핵심 플래그별 set/clear 경로

### `awaiting_approval`

| 방향 | 위치 | 조건 |
|------|------|------|
| set True | `core.py:457` | `is_approval(clean) and not awaiting_approval` — 승인 박스 감지 |
| set False | `receiver.py:171` | `approve_yes` + `is_approval(clean)`(정상) |
| set False | `receiver.py:181` | `approve_yes` + `is_alive`(late approved) |
| set False | `receiver.py:194` | `approve_yes` + session dead(재시작) |
| set False | `receiver.py:229` | `approve_no` + `is_approval(clean)`(정상) |
| set False | `receiver.py:241` | `approve_no` + `is_alive`(late denied) |
| set False | `receiver.py:254` | `approve_no` + session dead |

monitor 는 `awaiting_approval=True` 이면 **모든 flush/busy 처리를 건너뛰고 sleep(1)** 한다 (`core.py:462-464`). `_flush_completed` 자체도 맨 앞에서 조기 리턴 (`core.py:150-151`).

**⚠️ `cmd_esc` 에는 clear 경로가 없다** (`receiver.py:77-90`) — 이번 작업 Fix 2 의 타깃.

### `was_busy`

| 방향 | 위치 | 조건 |
|------|------|------|
| set True | `core.py:477` | busy 진입 edge (`busy_now and not was_busy`) |
| set False | `core.py:610` | post-compact 복구 후 |
| set False | `core.py:643` | settle 후 response flush 후 |
| set False | `core.py:261` | `_busy_watchdog` 종료 후 |

edge 검출 용도. `True`↔`False` 전이 시점에 pre-busy flush / 상태 메시지 삭제 / compaction 처리 등이 걸려있다.

### `saw_compaction` / `auto_compacting` / `compact_error_halted`

`core.py:283-290, 502-612` 에 집중. 세 플래그가 서로 맞물리며 다음 흐름을 만든다:

```
① Context limit 감지
   └─ auto_compacting = True → tmux.send_input("/compact")
   └─ sleep(2) 후 다음 tick
② busy 진입 (compact 수행 중)
   └─ was_busy = True, busy_started_at = now
   └─ has_compaction 감지 시 saw_compaction = True
③ /compact 가 API 에러로 실패하면
   └─ compact_error_halted = True
   └─ _send_model_switch_prompt 로 /model 전환 안내 → 사용자 입력 대기
④ busy 종료 + saw_compaction = True
   └─ post-compact flush (응답 복구) 또는 안내 메시지
   └─ saw_compaction = False, auto_compacting = False, was_busy = False
```

### `trust_ack_pending` / `resume_picker_ack_pending`

`core.py:344-366`. Claude Code 가 `Quick safety check` / `Resume from summary` 프롬프트를 띄우면 봇이 **자동으로 Enter** 를 눌러 기본값 승인. 플래그는 "이미 한 번 Enter 눌렀음 — 중복 방지" 용도이며 프롬프트가 사라지면 clear.

### `boot_notified`

`core.py:275, 194, 639`. 부팅 시 pane 에 이미 있던 블록은 `queue.seed` 로 "소비됨" 처리하되, 사용자에게 **최소 1번의 메시지** 는 전달되도록 보장하는 플래그. response flush 가 0 건이고 `boot_notified=False` 면 pane tail 30줄을 fallback 으로 송출 (`core.py:630-641`).

### `limit_reported`

`core.py:384, 389`. 한도 초과 메시지 중복 발송 방지. pane 에서 한도 문구가 사라지면 clear.

### `skip_permissions`

`core.py:49`, toggle 경로 `receiver.py:368-388`. `True` 면 Claude Code 기동 인자에 `--dangerously-skip-permissions` 추가 (`core.py:67-68`). 변경 시 `bridge.restart()` 가 세션을 재기동.

## `monitor()` 분기 우선순위

`core.py:313-645` 를 위에서 아래로 읽으면 우선순위가 곧 순서다:

```
1. tmux 세션 존재 체크 (has-session)        [dead_reported]
2. Claude 프로세스 alive 체크               [dead_reported]
3. trust prompt                             → auto Enter
4. resume picker                            → auto Enter
5. LIMIT_RE                                 → 안내 + sleep 30
6. compaction error                         → /model 전환 안내
7. context limit (+not halted)              → /compact dispatch
8. is_approval & !awaiting_approval         → pre-approval flush + 승인 박스 송출
9. awaiting_approval                        → sleep 1 (모든 flush 스킵)
10. busy edge (진입/지속/종료)
    10a. pre-busy flush                     → on edge
    10b. compaction 감지                    → saw_compaction
    10c. pane-hash 변화 추적                → busy_last_change_at
    10d. watchdog (timeout/stuck)           → _busy_watchdog
    10e. busy-stream flush                  → BUSY_STREAM_SEC 주기
    10f. 상태 메시지 edit
    10g. 종료 시 post-compact flush         → 복구/안내
11. settle 누적 → response flush            → SETTLE_TICKS=2
```

## 상태 저장 — `queue.reset()` 경로

`queue.reset()` 은 새 "user turn" 시작을 의미 (`stream_queue.py:101-104`). 호출 지점:

| 위치 | 맥락 |
|------|------|
| `core.py:274` | `monitor()` 진입 시 |
| `receiver.py:183` | `approve_yes` late approved |
| `receiver.py:204` | `approve_yes` session dead → 재시작 |
| `receiver.py:243` | `approve_no` late denied |
| `receiver.py:462` | `on_message` 이미지 수신 직전 |
| `receiver.py:479` | `on_message` 텍스트 수신 직전 |

**⚠️ `cmd_esc` 와 `approve_yes`/`approve_no` 의 정상 경로(`is_approval=True`)에는 `queue.reset()` 이 없다.** 정상 승인 후에는 Claude 가 이어서 응답을 내므로 같은 turn 으로 본다.

## `_state_lock`

`core.py:59`, `asyncio.Lock`. 현재 코드에서 실제로 `async with self._state_lock:` 으로 감싸지는 critical section 은 **없다** (grep 결과 0). 선언만 남아있음.

## Known Fragility

1. **상태 분산**. `awaiting_approval` clear 경로가 6곳에 흩어져 있으며 `cmd_esc` 누락 — 반복 계열 회귀의 근원.
2. **런타임 필드 늦은 set**. `monitor()` 진입 전에는 `self.was_busy` 등이 존재하지 않는다(`core.py:281-291`). `monitor()` 재진입 사이에 다른 코드에서 이 필드를 `getattr` 없이 읽으면 AttributeError 가능 (현재 접근 지점 없어 실질 영향 없음).
3. **`_state_lock` 미사용**. 직렬화 의도가 선언만 남고 실제로 동작하지 않음 — 다중 핸들러가 `bridge.queue` / `awaiting_approval` 을 동시에 건드릴 때의 race 여지.
4. **`last_approval_full` = `clean[-800:]`** (`core.py:448`) — 재연결 시 승인 컨텍스트 전달용. 현재 `last_approval_full` 은 저장만 되고 사용 지점이 없음 (grep 결과 set 만 존재).

## 참고

- 각 분기의 flush 시점: [02-streaming-pipeline.md](02-streaming-pipeline.md)
- 승인/모드별 실 플로우: [05-approval-flows.md](05-approval-flows.md)
