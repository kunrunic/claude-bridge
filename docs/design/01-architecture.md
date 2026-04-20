# 01. 런타임 아키텍처

MCP stdio 채널 기반, 멀티 세션을 지원하는 Telegram ↔ Claude Code 브리지 서버 토폴로지.

## 토폴로지

```
┌──────────────┐        ┌────────────────────────────────────┐
│              │ HTTPS  │  dispatcher.ts (Bun)               │
│ Telegram     │ long-  │  ├─ Registry  (active sessions)    │
│ Bot API      │ polling│  ├─ IPC srv   (unix socket)        │
│              │        │  ├─ Poller    (inbound convert)    │
└──────────────┘        │  └─ Observer  (pane status)        │
       │                └──────────────┬─────────────────────┘
       │                  async call   │  IPC over unix socket
       │                               ▼
       │        ┌────────────────────────────────────┐
       │        │  MCP stdio server (server.ts)      │
       │        │  (one per Claude Code session)     │
       │        │  ├─ tools: reply, react, edit...   │
       │        │  └─ notifications: inbound, perm...│
       │        └──────┬─────────────┬───────────────┘
       │        publish│             │ subscribe
       │               │             │
       │ Telegram      │             │
       │ inbound   ◄───┘             ▼
       │ + polling              Claude Code
       └─────────────────────►  (tmux pane)
                                 capture-pane
```

## 세 계층의 분리

**공용 핵심 로직** (`src/core/`)
- `dispatcher-core.ts` — spawnSession, killSession, handleSlash 순수 함수
- `registry.ts` — active session 추적, 상태 저장
- `slash.ts` — 커맨드 파싱 및 BOT_COMMANDS 목록
- `sessions.ts` — ~/.claude/projects 스캐너, 세션 복원
- `lifecycle.ts` — PID lock (중복 실행 방지), orphan watchdog, graceful shutdown
- `observer.ts` — pane tail 분석으로 busy/idle/error 신호 감지
- `ipc.ts` — unix socket 프로토콜 (dispatcher ↔ MCP server)
- `anomaly.ts` — always-on JSONL 로거

**Telegram 채널 구현** (`src/channels/telegram/`)
- `server.ts` — MCP stdio 서버. 도구 4개(reply, react, edit_message, download_attachment) 등록, permission 알림
- `poller.ts` — grammy 래퍼, inbound 메시지 → IPC 메시지 변환, token collision 감지
- `client.ts` — grammy 클라이언트 래퍼
- `permissions.ts` — InlineKeyboard 빌드, 권한 요청 UI 포매팅
- `access.ts` — allowlist 게이트 (userId/chatId)
- `config.ts` — ~/.claude-bridge/config.json 로드, 권한 강화
- `inbox.ts` — 첨부파일 저장

**진입점**
- `dispatcher.ts` — main 함수. config 로드, 모든 계층 조립, poller/ipc/observer 시작

## 채널 무관성 원칙

`src/core/*` 는 Telegram을 몰라야 한다:
- `dispatcher-core.ts` — 순수 함수. Session, Registry, SlashCommand만 다룸 (`src/core/dispatcher-core.ts:37-168`)
- `handleSlash` 콜백은 dependency injection (spawn, resume, kill, listRecent, renderStatus) (`dispatcher-core.ts:87-97`)
- `registry.ts` 는 session state만 관리. Telegram API 호출 없음

`src/channels/telegram/*` 는 dispatcher와 분리:
- MCP server는 dispatcher와 IPC 소켓으로 통신 (`server.ts:136`)
- 권한 요청은 permission_request IPC 메시지 → dispatcher가 Telegram 전송 (`dispatcher.ts:100-122`)

## 멀티 세션 아키텍처

- **per-session tmux** — 각 Claude Code 세션은 자신의 tmux 세션 (이름: `cb-s{N}`) (`registry.ts:49`)
- **per-session MCP server** — Claude Code 실행 시 새 MCP server stdio 시작. 환경변수로 session_id 전달 (`dispatcher-core.ts:56-58`)
- **dispatcher 중앙 수집** — 모든 session의 IPC 소켓을 dispatcher가 관리 (`dispatcher.ts:67-78`)
- **active session 추적** — Registry가 현재 활성 세션 관리 (`registry.ts:39-41`)

## 외부 의존

- **tmux** (`src/core/tmux/session.ts`) — new-session, kill-session, send-keys, capture-pane 래퍼 (`session.ts:27-101`)
- **Claude Code CLI** — `claude --disallowedTools ... --allowedTools ... --channels server:tg_channel` 명령어로 실행 (`dispatcher-core.ts:51-53`)
- **파일 시스템**
  - `~/.claude/projects/*.jsonl` — Claude Code 세션 파일 (복원 용)
  - `~/.claude-bridge/workspaces/` — 브리지 세션 cwd 격리 디렉토리 (`dispatcher.ts:64`)
  - `~/.claude-bridge/dispatcher.sock` — IPC unix socket (`ipc.ts:7`)
  - `~/.claude-bridge/anomaly.jsonl` — always-on 로그 (`anomaly.ts:13`)
  - `~/.claude-bridge/telegram/bot.pid` — polling lock (`lifecycle.ts:11`)
- **grammy** (Telegram 클라이언트) — polling 모드, callback_query/message 핸들러

## 워크스페이스 격리

각 세션은 `~/.claude-bridge/workspaces/<label>/` 디렉토리에서 실행되거나, 명시적 cwd 전달 가능:
- `/new` — 새 세션, 기본값 `~/.claude-bridge/workspaces/bot/`
- `/resume <id>` — 기존 세션 복원. ~/.claude/projects 에서 저장된 cwd 복구
- 모든 세션의 클라이언트는 자신의 MCP server에 접속 (IPC로는 dispatcher만 도달) (`server.ts:136`)

## 설정

`~/.claude-bridge/config.json`:
```json
{
  "botToken": "string (required)",
  "allowlist": ["user_id_1", "chat_id_1"],
  "defaultChatId": "string (optional, for announcements)",
  "dumpEnabled": false
}
```

파일 권한은 자동 강화: 디렉토리 0700, 파일 0600 (`config.ts:17-24`)

## Known Fragility

- 특이사항 없음.

## 참고

- IPC 프로토콜 상세: [02-mcp-channel-protocol.md](02-mcp-channel-protocol.md)
- 세션 생명주기: [03-session-lifecycle.md](03-session-lifecycle.md)
- 슬래시 커맨드: [04-slash-commands.md](04-slash-commands.md)
