# 06. 관측성 (로그, dump)

봇이 "무엇을 했는지" 를 재구성하는 두 채널이 있다: **포그라운드 로그**(기본)와 **dump**(옵트인).

## 포그라운드 로그

`bridge/config.py:52-58` 의 `_log(tag, msg)`:

```python
def _log(tag: str, msg: str = ""):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{tag}] {msg}" if msg else f"[{ts}] [{tag}]")
```

형식: `[HH:MM:SS] [TAG] msg`. `start.sh` 가 stdout 을 `logs/<date>.log` 로 파이프한다(bugreport/logs/ 참고).

### 주요 태그

| 태그 | 위치 | 의미 |
|------|------|------|
| `BOOT` | bot.py:179 | 봇 시작 |
| `SHUTDOWN` / `POLLING-ERROR` / `POLLING-RETRY` / `POLLING-FATAL` | bot.py:182-190 | Telegram 폴링 재연결 상태 |
| `USER→BOT` | receiver.py 전역 | 사용자 메시지/명령 수신 |
| `BOT→AI` | receiver.py:464,481 | 사용자 입력을 Claude 로 포워드 |
| `USER→AI` | receiver.py:86, 106-109 | 키/명령 주입 (ESC, /model …) |
| `USER-ACK` | receiver.py:172, 182, … | 승인/거부 버튼 처리 결과 |
| `AI-APPROVAL` | core.py:449 | 승인 박스 감지 (도구명 포함) |
| `AI-BUSY` | core.py:490 | busy 진입 (상태 라벨 포함) |
| `AI→BOT` | core.py:175, 635 | 블록 송출 준비 |
| `BOT→USER` | core.py:184, 638 | Telegram 전송 성공 |
| `QUEUE-SLIP` | core.py:155 | StreamQueue slip 감지 |
| `AI-CTX-LIMIT` / `AI-COMPACT` / `AI-COMPACT-ERR` / `AI-COMPACT-NOSEND` | core.py 여러 곳 | 컨텍스트/압축 이벤트 |
| `AI-WATCHDOG` | core.py:229 | busy timeout/stuck 트리거 |
| `AI-LIMIT` | core.py:382 | 사용량 한도 도달 |
| `MONITOR` / `MONITOR-ERROR` | core.py 전역 | monitor 생명주기/예외 |
| `AUTO-ACK` | core.py:346, 358 | 자동 Enter (trust/resume picker) |
| `BOOT-SEED` | core.py:306 | 부팅 시 queue seed |
| `FLUSH-SEND-FAIL` / `SEND-HTML-FAIL` / `SEND-PLAIN-FAIL` | core.py:187, sender.py:55-61 | 전송 실패 |
| `LOCK-ERROR` / `LOCK-RELEASE-ERROR` / `UNLOCK` / `MY-LOCKS-ERROR` | session.py | 인스턴스 락 |
| `TMUX-TIMEOUT` | tmux.py:44 | tmux subprocess 5초 초과 |
| `STATUS-MSG-ERR` | core.py:496 | Telegram 상태 메시지 편집 실패 |

### 로그 기반 증상 분류법

- `USER→BOT` 대비 `BOT→USER` 카운트가 극단적으로 낮다 → flush 경로 장애 (이번 ESC flush 회귀 패턴).
- `AI-BUSY` edge 가 반복되는데 `AI→BOT` 가 없다 → 응답은 생성됐지만 큐/경계 문제로 추출 실패.
- `QUEUE-SLIP` 이 계속 찍힌다 → scrollback eviction 이 계속 발생 (대량 출력 세션).
- `MONITOR-ERROR` 5회 → 사용자 알림, 10회 → monitor 종료 (`core.py:652-663`).

## dump (`CB_DUMP=1`)

`bridge/dump.py`. env 로 켜지는 구조화된 증거 채널. 꺼져있으면 모든 함수 no-op.

### 디렉토리 구조

```
dump/
 └─ YYYYMMDD/
     └─ HHMMSS_<instance>/
        ├─ pane_tick.jsonl    # 0.5s 주기 pane 캡처 (hash dedup)
        └─ events.jsonl       # 각 컴포넌트의 이벤트
```

보관 3일(`_RETENTION_DAYS=3`), 부팅 시 오래된 디렉토리 자동 삭제 (`dump.py:68-82`).

### `pane_tick.jsonl`

`dump.py:129-149` `pane_snapshot`. 레코드:

```json
{"ts": "...", "hash": "md5", "busy": bool, "awaiting_approval": bool,
 "raw": "<trimmed>", "clean": "<trimmed>"}
```

`raw`/`clean` 은 각각 8000자 초과 시 꼬리 잘림. md5 동일하면 저장 스킵 (idle 압축).

### `events.jsonl`

`dump.py:106-120` `event(source, kind, **fields)`. `{ts, source, kind, ...}` 한 줄씩. 주요 source/kind:

| source | kind | 필드 | 의미 |
|--------|------|-----|------|
| `tmux` | `send_input` | `text`, `length` | Claude 에 주입된 텍스트 |
| `tmux` | `send_key` | `key` | ESC/Enter/Up/Down 등 |
| `sender` | `send_output` | `length`, `chunks`, `preview` | 사용자로 송출된 본문 |
| `sender` | `send_approval` | `preview` | 승인 박스 스니펫 |
| `sender` | `send_html_fail` / `send_plain_fail` | `error` | 전송 실패 |
| `receiver` | `on_message` | `has_photo`, `caption`, `length` | 사용자 메시지 수신 |
| `receiver` | `callback` | `data`, `chat_id` | 버튼 콜백 |
| `core` | `monitor_start` / `monitor_cancelled` | `chat_id`, `session_id` | monitor 생명주기 |
| `core` | `queue_slip` | `tag`, `slip_kind`, `idx`, `cc`, `last_fp` | slip 감지 |
| `core` | `flush_peek` | `tag`, `include_last`, `completed_count`, `new_count`, `queue_idx`, `slip` | flush 판정 결과 |
| `core` | `flush_block` | `tag`, `length`, `preview` | 실제 송출한 블록 미리보기 |
| `core` | `flush_send_fail` | `tag`, `error` | 송출 실패 |
| `dump` | `init` / `tick_started` / `tick_stopped` / `tick_error` | — | dump 자체 생명주기 |

### 교차 대조 기법

- **스트리밍 누락 조사**: `pane_tick.jsonl` 에서 `⏺` 블록 시각을 뽑고 `events.jsonl` 의 `flush_block.preview` 와 대조 → pane 에 있었지만 송출되지 않은 블록 식별.
- **경계 오탐 확증**: `completed_count=0, new_count=0, slip=null` 이 `tag="response"` 로 연속 찍히면 `_response_region` 오탐 시나리오.

## 버그 리포트 수집

`bin/bugreporter.sh` (추정 — 파일 별도) 가 다음을 한 타임스탬프 디렉토리로 묶는다:

```
bugreport/YYYYMMDD_HHMMSS/
  ├─ BUG_REPORT.md          # 분석 산출물
  ├─ CLAUDE.md              # 세션 목적 지시서
  ├─ tmux_capture.txt       # 최근 pane 500줄 + 세션 상태
  ├─ logs/<date>.log        # 해당 시각 포그라운드 로그
  ├─ config_masked.json     # 토큰 마스킹한 설정
  ├─ bot.py, bridge/*.py    # 소스 스냅샷
  ├─ git_status.txt         # git 상태/최근 커밋
  └─ dump/                  # 있을 때만 (CB_DUMP=1 세션이었을 경우)
```

기존 리포트 예시: `bugreport/20260417_095801/`, `bugreport/20260418_094744/` 등.

## Known Fragility

1. **로그 스팸 없음 ≠ 정상 동작**. `take_new` 가 `[]` 를 리턴하는 경로는 `for blk in new_blocks:` 본체가 돌지 않아 `AI→BOT` / `BOT→USER` 로그가 전혀 남지 않는다. `flush_peek` 이벤트는 dump 에 찍히지만 CB_DUMP 가 꺼진 상태에서는 관측 불가.
2. **로그 레벨 없음**. 모든 `_log` 가 `print` — 중요도 필터가 없음. `logs/` 가 누적되면 수동 grep 외 분석 불가.
3. **dump 파일 버퍼링**. `buffering=1` (line buffered) 로 열지만 flush 는 OS 의 line buffer 정책에 의존. 비정상 종료 시 마지막 몇 레코드 유실 가능.
4. **dump 경로 고정**. `bridge/dump.py:60` 이 `<repo>/dump` 에 하드코딩. 배포 환경에서 별도 경로 구성 불가.

## 참고

- 증거 기반 버그 분석 예시: `bugreport/20260418_094744/BUG_REPORT.md`
- 파이프라인의 flush 종류: [02-streaming-pipeline.md](02-streaming-pipeline.md)
