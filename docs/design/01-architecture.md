# 01. 런타임 아키텍처

## 토폴로지

```
┌──────────────┐        ┌────────────────────────────────────┐
│              │ HTTPS  │  bot.py (python-telegram-bot)      │
│ Telegram     │ ◄────► │  ├─ Application (long-polling)     │
│ Bot API      │        │  └─ Handlers: cmd_*, on_message,   │
│              │        │                on_callback         │
└──────────────┘        └──────────────┬─────────────────────┘
                                       │ in-process call
                                       ▼
                        ┌────────────────────────────────────┐
                        │  bridge.core.Bridge (싱글톤)        │
                        │  ├─ monitor(): asyncio task         │
                        │  ├─ queue: StreamQueue              │
                        │  └─ state flags (awaiting_approval, │
                        │                  was_busy, …)       │
                        └──────────┬─────────────────┬────────┘
                    capture-pane   │                 │ send-keys
                         ◄─────────┘                 └────────►
                        ┌─────────────────────────────────────┐
                        │  tmux session "claude_bridge"       │
                        │  └─ pane 0: Claude Code TUI         │
                        │     (`claude [--resume <id>]        │
                        │       [--dangerously-skip-permissions]`)│
                        └─────────────────────────────────────┘
```

- **Claude API 를 직접 쓰지 않는다.** Claude Code TUI 를 tmux pane 에 띄우고 `capture-pane` 로 화면을 읽어 응답을 추출한다 (`bridge/tmux.py:57-69`).
- **1:1 싱글톤** — `bridge.core.bridge` 는 모듈 전역 인스턴스. chat_id ↔ tmux 세션 ↔ Claude 세션은 1:1 로 가정한다 (`bridge/core.py:674-675`). 다중 사용자 지원 아님.
- **인스턴스 격리** — tmux 세션 이름을 `tmux_session` 설정값(기본 `claude_bridge`)으로 잡아 `_ts()`/`_tp()` 로 exact-match 만 허용. sibling 세션 오인 조작 방지 (`bridge/tmux.py:16-34`).

## 모듈 레이어링

`bridge/__init__.py:3-12` 에 선언된 단방향 의존 순서:

```
config   — 상수/설정/로거/권한 게이트
tmux     — tmux 원시 조작 (subprocess)
parser   — pure 함수 (ANSI strip, 상태 판정, ⏺ 블록 추출)
session  — Claude 세션 JSONL 검색 + 락
sender   — Telegram outbound (_send_output, _send_approval, …)
core     — Bridge + monitor 루프
receiver — Telegram inbound (cmd_*, on_message, on_callback)
```

테스트가 특정 심볼을 patch 할 때 **정의 위치 기준**으로 패치하도록, sibling 모듈은 반드시 `<module>.<name>` 형태로 호출한다 (`bridge/core.py:6-7`, `bridge/receiver.py:6-7`).

### 계층 위반 금지선

- `parser.py` 에 `tmux`/`sender`/`telegram` import 금지 (`bridge/parser.py:1-5`).
- `sender.py`/`receiver.py` 가 서로 import 하지 않음 (동일 레이어).
- `bot.py` 는 엔트리 포인트일 뿐, 로직을 담지 않음 — Handler 바인딩과 폴링 루프만 (`bot.py:144-199`).

## 엔트리 포인트 `bot.py`

| 단계 | 위치 | 역할 |
|------|------|------|
| 부트 | `bot.py:166-199` | `dump.init` → `_build_app` → `app.run_polling` (exponential backoff 재연결) |
| 핸들러 등록 | `bot.py:144-163` | CommandHandler(`/start`, `/end`, `/esc`, `/model`, `/unlock`, `/whoami`) + CallbackQuery + Message |
| post_init | `bot.py:127-141` | 부팅 시 tmux 세션이 이미 있으면 `bridge.monitor` 재부착 |
| 재-export | `bot.py:30-122` | 기존 테스트가 `bot.<symbol>` 로 참조하는 모든 심볼을 re-export |

## 외부 의존

- **tmux** (`/opt/homebrew/bin/tmux` 고정 — `bridge/tmux.py:40`). timeout 5초로 모든 subprocess 를 감쌈.
- **Claude Code CLI** — `config.json` 의 `claude_path`. `--resume <id>` 와 `--dangerously-skip-permissions` 플래그를 조합 (`bridge/core.py:63-69`).
- **파일 시스템** — `~/.claude/projects/*.jsonl` (세션), `~/.claude/.cb_lock_<id>` (인스턴스간 락, `bridge/session.py:17-23`).
- **telegram-bot** 라이브러리 — polling + ApplicationBuilder. HTTPXRequest 의 connect/read timeout 10/15초 (`bot.py:148`).

## 설정 (`config.json`)

`bridge/config.py:37-49`:
- `token` (Telegram Bot) — 필수
- `claude_path` — 필수
- `allowed_ids: [int]` — 권한 게이트; 비어있으면 모든 inbound 가 `deny()`
- `tmux_session` — 기본 `"claude_bridge"`

상수는 모두 `bridge/config.py:11-22` 에 모임. 대표값:

| 이름 | 값 | 용도 |
|------|-----|------|
| `TMUX_SCROLL_LINES` | 500 | `capture-pane -S -N` 의 N |
| `APPROVAL_SCAN_LINES` | 30 | `_approval_box` / `summarize_approval` 스캔 |
| `BUSY_CHECK_TAIL` | 20 | `is_busy` fallback tail |
| `BUSY_STREAM_SEC` | 10 | bypass 모드 ⏺ 스트리밍 간격 |
| `BUSY_TIMEOUT_SEC` | 600 | watchdog 총 시간 상한 |
| `BUSY_STUCK_SEC` | 300 | pane 변화 없음 watchdog |
| `SETTLE_TICKS` | 2 | idle 안정화 틱 수 |
| `MAX_MSG_CHARS` | 3500 | Telegram chunking |

## Known Fragility

- `tmux_session` 을 `claude_bridge` 로 두고 `claude_bridge2` 같은 sibling 을 함께 돌릴 때, prefix-match 로 오인 조작된 사고가 있었음 → 현재 `_ts()`/`_tp()` 로 exact-match 강제 (`591f553`).
- `post_init` 의 재부착(`bot.py:137-141`) 은 `ALLOWED_IDS` 의 **첫번째** chat_id 로만 — 다중 사용자 미지원의 실질적 단서.

## 참고

- 데이터 흐름 상세: [02-streaming-pipeline.md](02-streaming-pipeline.md)
- 상태 플래그: [04-state-machine.md](04-state-machine.md)
- 관측: [06-observability.md](06-observability.md)
