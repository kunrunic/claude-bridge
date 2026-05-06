# 01. 런타임 아키텍처

채널 추상화 위에 멀티 세션 Claude Code 브리지를 구성하는 토폴로지.
현재 Telegram, cb CLI/cb-menu, tmux status bar 세 채널이 같은 dispatcher 위에서 공존한다.

## 토폴로지

```
                         dispatcher.ts (Bun, always-on)
                         ├─ Registry        (active sessions, pin state)
                         ├─ IPC server      (unix socket, sessions / cb-menu)
                         ├─ TickObserver    (5s tick — busy/idle/rate/compact)
                         ├─ Animation FSM   (busy emoji rotation)
                         └─ Channel fan-out

       ┌────────────────────┬─────────────────────┬──────────────────┐
       │                    │                     │                  │
       ▼                    ▼                     ▼                  ▼
┌────────────┐      ┌──────────────┐      ┌──────────────┐    ┌──────────────┐
│ Telegram   │      │ cb-menu      │      │ tmux status  │    │ MCP servers  │
│  Channel   │      │ Channel      │      │ Channel      │    │ (per-session │
│            │      │ (Ink TUI)    │      │ (minimap)    │    │  bridge-     │
└──────┬─────┘      └──────┬───────┘      └──────┬───────┘    │  channel)    │
       │                   │                     │            └──────┬───────┘
HTTPS  │                   │ UDS RPC             │ tmux            │ stdio
long-  │                   │                     │ set-option      │ + IPC UDS
poll   ▼                   ▼                     ▼                  ▼
   Telegram            ssh client            tmux server       Claude Code
   Bot API             ($ cb home)           (cb-menu          (tmux pane,
                                              + cb-s1...sN)     per session)
```

각 채널은 같은 `Channel` 인터페이스를 구현한다 — dispatcher 본체는 채널 종류를 모른 채 같은 SessionEvent 를 모든 채널에 fan-out 한다. 자세히는 [08-channel-abstraction.md](08-channel-abstraction.md).

---

## 세 채널의 책임

| 채널 | 입력 (사용자 → dispatcher) | 출력 (dispatcher → 사용자) | 위치 |
|---|---|---|---|
| **Telegram** | 메시지·버튼 → InboundEvent / PermissionReply | 채팅 메시지·이모지·pin·인라인 키보드 | `src/channels/telegram/` |
| **cb-menu** | 키보드 입력 (TUI 안) → cli_request RPC | 세션 리스트·minimap·picker 화면 | `src/channels/cli/menu/` |
| **tmux status** | (입력 없음 — 출력 전용) | 각 cb-* tmux 세션의 status bar minimap | `src/channels/tmux/` |
| **MCP servers** | reply / react / edit / download — Claude 의 도구 호출 | (도구 결과만 — 사용자 노출 X) | `src/channels/telegram/server.ts` |

> **MCP server 는 채널이 아니다** — Claude 가 Telegram 과 대화하기 위한 inner protocol 이고, dispatcher 와는 IPC 로 통신. 위 표는 user-facing 채널 3개 + Claude-facing IPC 채널 1개를 함께 보여준다.

---

## 계층 구분

### `src/core/` — 채널 무관 핵심

dispatcher 본체와 모든 채널이 공유하는 도메인 로직.

| 파일 | 역할 |
|---|---|
| `channel.ts` | Channel interface + SessionEvent / InboundEvent / PermissionRequest 타입 정의 |
| `dispatcher-core.ts` | spawnSession, killSession, handleSlash 순수 함수 |
| `dispatcher-handlers.ts` | IPC 메시지 핸들러 (hello / permission_request / inbound) |
| `SessionManager.ts` | spawn / resume / fork / gracefulKill, dialog 자동 dismiss |
| `TickObserver.ts` | 5초 주기 pane 관찰, rate_limit / compact / context_limit 감지 |
| `registry.ts` | active session 추적 + activePin / pinnedReplies 영속화 |
| `slash.ts` | 커맨드 파서 + BOT_COMMANDS 목록 |
| `sessions.ts` | `~/.claude/projects` 스캐너 (resume picker 캐시) |
| `lifecycle.ts` | PID lock, orphan watchdog, graceful shutdown |
| `observer.ts` | pane tail 정규식 (BUSY_ACTIVE_RE 등) |
| `ipc.ts` | unix socket 프로토콜 (dispatcher ↔ MCP server / cb-menu) |
| `anomaly.ts` | always-on JSONL 로거 |
| `channel-prompt.ts` | 채널 지시 프롬프트 + mcp.json 생성 |
| `pin.ts` | active session pin helper (Telegram 채널이 사용) |
| `tmux/session.ts` | tmux wrapper (new-session / send-keys / capture-pane) |
| `tmux/status.ts` | tmux status bar 조작 (set-status-right 등) |

`src/core/*` 는 Telegram 도, cb 도, tmux 도 모른다. 같은 SessionEvent / Channel 인터페이스만 다룬다.

### `src/channels/telegram/` — Telegram 채널

| 파일 | 역할 |
|---|---|
| `channel.ts` | TelegramChannel — Channel 구현 (notify / announce / requestPermission) |
| `server.ts` | per-session MCP stdio 서버 (도구 4개 등록) |
| `IpcBridge.ts` | MCP server ↔ dispatcher IPC (자동 재연결) |
| `SlashHandler.ts` | 슬래시 커맨드 + 인라인 키보드 |
| `tools/definitions.ts` | TOOL_LIST 스키마 (reply / react / edit_message / download) |
| `tools/ToolHandler.ts` | 도구 호출 디스패치 |
| `poller.ts` | grammy 래퍼, inbound → IPC 변환 |
| `client.ts` | grammy 클라이언트 |
| `permissions.ts` | InlineKeyboard (Allow / Deny) |
| `access.ts` | allowlist 게이트 |
| `inbox.ts` | 첨부파일 저장 |
| `config.ts` | config 로드, 권한 강화 |

자세히는 [02-mcp-channel-protocol.md](02-mcp-channel-protocol.md), [04-slash-commands.md](04-slash-commands.md), [05-access-and-permissions.md](05-access-and-permissions.md).

### `src/channels/cli/` — cb 클라이언트 + cb-menu

cb 클라이언트(SSH wrapper)와 호스트의 cb-menu TUI. 두 개를 따로 본다:

| 파일 | 역할 |
|---|---|
| `cli.ts` | cb 명령 진입점 (list / add / connect / start·stop·restart) |
| `hosts.ts` | `~/.cb/hosts.json` 영속, `~/.ssh/config` 동기화 |
| `menu/index.tsx` | cb-menu Ink 앱 진입점 |
| `menu/App.tsx` | sessions / new / resume 모드 라우팅 |
| `menu/SessionList.tsx` | 세션 리스트 (↑↓ Enter / 1-9 / n r x q) |
| `menu/DirBrowser.tsx` | new 모드 디렉토리 브라우저 |
| `menu/ResumePicker.tsx` | resume 모드 type-ahead picker |
| `menu/keybindings.ts` | F-key tmux bind (F1 menu / F2 new / F5 back / F6 handoff) |
| `menu/rpc.ts` | cb-menu → dispatcher RPC (cli_request) |
| `menu/spinner.ts` | SPINNER_FRAMES 공유 (animation 일관성) |

자세히는 [07-cli-channel.md](07-cli-channel.md).

### `src/channels/tmux/` — tmux status bar 채널

minimap 출력 전용 채널. 입력은 없음.

| 파일 | 역할 |
|---|---|
| `channel.ts` | TmuxStatusChannel — Channel 구현. notify 시 minimap 갱신 |
| `render.ts` | renderMinimap (pure function, 테스트 가능). sigil 매핑 + spinner |

각 cb-* tmux 세션의 status-right 에 `▶s1·my-app  s2⠋backend` 같은 텍스트를 set-option 으로 갱신.

### 진입점

| 파일 | 역할 |
|---|---|
| `dispatcher.ts` | main — config 로드, 모든 채널 조립, IPC server 기동, animation FSM |

---

## 멀티 세션 모델

- **per-session tmux** — 각 Claude Code 세션은 자신의 tmux 세션 (`cb-s{N}`). cb-menu 도 별도 tmux 세션 (`cb-menu`)
- **per-session MCP server** — Telegram 모드에서 spawn 시 새 MCP stdio server. `CB_SESSION_ID` 환경변수로 식별
- **registry 중앙 추적** — 모든 세션의 state / signal / label 을 dispatcher 가 관리. `registry.json` 으로 영속
- **active session 단일 지정** — 한 시점에 하나만 active. Telegram 메시지·승인이 active 세션으로 라우팅

CB_INSTANCE 멀티 인스턴스: tmux 이름 prefix 가 `cb-<instance>-` 가 되어 인스턴스끼리 충돌 회피.

---

## 워크스페이스 격리

각 세션은 자기 cwd 에서 실행:

- `/new [cwd]` — 사용자 지정 cwd, 미지정 시 기본 workspace
- `/resume <id>` — `~/.claude/projects` 에서 원본 cwd 복구
- `/fork <id>` — 컨텍스트는 상속하되 새 session_id 부여 (label 만 유지)

자세히는 [03-session-lifecycle.md](03-session-lifecycle.md).

---

## 외부 의존

- **tmux** — `src/core/tmux/session.ts` 가 `new-session`, `kill-session`, `send-keys`, `capture-pane` 래핑
- **Claude Code CLI** — `claude --mcp-config <mcp.json> --append-system-prompt-file <channel-prompt> ...` 형태로 실행 (`dispatcher-core.ts`)
- **grammy** — Telegram 클라이언트 (long-polling 모드)
- **Ink** — cb-menu 의 React 기반 터미널 UI

---

## 파일 시스템

| 경로 | 내용 |
|---|---|
| `~/.claude/projects/*.jsonl` | Claude Code 세션 영속 (resume 소스) |
| `~/.claude-bridge/config.json` | botToken / allowlist / defaultChatId |
| `~/.claude-bridge/registry.json` | active session 목록 + activePin / pinnedReplies |
| `~/.claude-bridge/dispatcher.sock` | IPC unix socket (cb-menu / MCP server 가 연결) |
| `~/.claude-bridge/dispatcher.pid` | PID lock (이중 기동 방지) |
| `~/.claude-bridge/anomaly.jsonl` | always-on 이상 신호 로거 (rotating) |
| `~/.claude-bridge/logs/` | dispatcher 시간대별 로그 |
| `~/.claude-bridge/workspaces/` | bridge 세션 기본 cwd |
| `~/.claude-bridge/telegram/inbox/` | Telegram 첨부파일 |
| `~/.claude-bridge/telegram/bot.pid` | polling lock |
| `~/.cb/hosts.json` | cb 호스트 등록 (cb 클라이언트가 사용) |

권한 자동 강화: 디렉토리 0700, 파일 0600.

---

## 채널 무관성 원칙

`src/core/*` 는 어떤 채널의 구현 상세도 알지 않는다:

- `dispatcher-core.ts` 의 `handleSlash` 는 spawn / resume / kill / listRecent / renderStatus 콜백만 받음. Telegram 의 InlineKeyboard 같은 것 X
- `registry.ts` 는 세션 상태만 관리. Telegram API 호출 X
- 모든 채널이 `Channel.notify(SessionEvent)` 를 통해서만 dispatcher 와 대화

이 원칙 덕분에 새 채널(Slack, Discord, 웹 UI 등) 추가는 `src/channels/<name>/` 디렉토리 하나만 만들면 된다 — `src/core/` 변경 없이.

자세히는 [08-channel-abstraction.md](08-channel-abstraction.md).

---

## Known Fragility

- TmuxStatusChannel 갱신 주기가 dispatcher event-driven 이라, signal 변화 사이 idle 구간엔 spinner frame 이 멈춘 듯 보임. 의도적 trade-off (CPU/tmux call 절약).

---

## 참고

- 채널 추상화: [08-channel-abstraction.md](08-channel-abstraction.md)
- MCP 채널 프로토콜: [02-mcp-channel-protocol.md](02-mcp-channel-protocol.md)
- 세션 생명주기: [03-session-lifecycle.md](03-session-lifecycle.md)
- 슬래시 커맨드: [04-slash-commands.md](04-slash-commands.md)
- cb CLI / cb-menu / tmux status: [07-cli-channel.md](07-cli-channel.md)
