# 02. MCP 채널 프로토콜

Claude Code `claude/channel` MCP 프로토콜 표면. dispatcher와 server 간 IPC 메시지, MCP 도구와 알림 스키마.

## 도구 4개 (Tools)

### reply
Telegram 메시지 송신 (텍스트 ± 첨부파일).

**파라미터** (`server.ts:33-41`)
- `chat_id` (string, required) — Telegram chat ID
- `text` (string, required) — 메시지 본문. 최대 4096자, 초과 시 자동 분할
- `reply_to` (string, optional) — 특정 메시지에 리플
- `reply_to_mode` ("off" | "first" | "all", default "all") — 분할 시 threading
- `files` (string[], default []) — 절대 경로 배열 (이미지 → photo, 기타 → document)
- `format` ("text" | "markdownv2", default "text")
- `split` ("newline" | "length", default "newline") — 초과 길이 분할 전략

**구현** (`server.ts:250-310`)
- Telegram API `sendMessage`, `sendPhoto`, `sendDocument` 호출
- 경로 검증: `.claude-bridge`, `.claude/channels` 디렉토리는 거부 (`server.ts:98-109`)

### react
메시지에 이모지 반응 추가.

**파라미터** (`server.ts:43-47`)
- `chat_id` (string)
- `message_id` (string)
- `emoji` (string) — 고정 화이트리스트 (👍 ❤ 🔥 등)

### edit_message
기존 메시지 편집 (알림 푸시 없음).

**파라미터** (`server.ts:49-54`)
- `chat_id` (string)
- `message_id` (string)
- `text` (string)
- `format` ("text" | "markdownv2", default "text")

### download_attachment
메시지 첨부파일 다운로드.

**파라미터** (`server.ts:56-58`)
- `file_id` (string) — Telegram 파일 ID

**반환**
- Absolute 로컬 경로 (date-stamped directory에 저장) (`inbox.ts:20-38`)

## 알림 (Notifications)

### notifications/claude/channel
인바운드 메시지 또는 Telegram 폴링 이벤트를 Claude로 전송.

**파라미터** (`server.ts:157-162`)
- `content` (string) — 메시지 본문
- `meta` (object) — 메타데이터
  - `chat_id` (string)
  - `message_id` (string, optional)
  - `user` (string) — username 또는 ID
  - `user_id` (string)
  - `ts` (string) — ISO 8601 타임스탬프
  - `image_path` (string, optional) — 로컬 이미지 경로
  - `attachment_kind` (string, optional) — "document"
  - `attachment_file_id` (string, optional)
  - `attachment_size` (string, optional)
  - `attachment_mime` (string, optional)
  - `attachment_name` (string, optional)

**경로**
- **IPC 모드** (dispatcher 감시 중): IPC 메시지 via dispatcher → poller (`server.ts:172-189`)
- **스탠드얼론** (dispatcher 없음): grammy 폴링 직접 (`server.ts:198-202`)

### notifications/claude/channel/permission
권한 요청 UI를 Claude에 제시.

Claude는 도구 호출 권한 필요 시 이를 호출하면, server가 알림으로 사용자 승인 UI 표시.

**파라미터** (`server.ts:205-247`)
- `request_id` (string)
- `tool_name` (string)
- `description` (string)
- `input_preview` (string) — JSON 미리보기

**처리 경로**
- IPC 모드: dispatcher로 permission_request 메시지 전송 (`server.ts:221-229`)
- 스탠드얼론: 직접 Telegram InlineKeyboard 전송 (`server.ts:234-244`)

## IPC 프로토콜 (dispatcher ↔ server)

Unix socket 기반, JSON 라인 포맷. 메시지 타입:

### hello
server → dispatcher, 세션 등록.

```json
{ "op": "hello", "session_id": "s1", "pid": 1234 }
```

**처리** (`dispatcher.ts:92-99`)
- Registry에 session_id ↔ socketId 등록
- dispatcher가 이 socket으로 inbound 메시지 전송

### inbound
dispatcher → server, Telegram 인바운드 메시지 중계.

```json
{
  "op": "inbound",
  "content": "hello world",
  "meta": { "chat_id": "123", "user_id": "456", ... }
}
```

**경로** (`dispatcher.ts:331-336`)
- 활성 세션의 socket에 전송
- server가 `notifications/claude/channel` 알림으로 변환

### permission_request
server → dispatcher, Claude가 도구 권한 요청할 때.

```json
{
  "op": "permission_request",
  "session_id": "s1",
  "request_id": "abc12",
  "tool_name": "mcp__tg_channel__reply",
  "description": "...",
  "input_preview": "{...}"
}
```

**처리** (`dispatcher.ts:100-122`)
- allowlist의 모든 chat_id에 Telegram InlineKeyboard 메시지 전송
- 사용자가 ✅/❌ 누르면 permission_reply 메시지 송신

### permission_reply
dispatcher → server, 사용자가 권한 승인/거절.

```json
{
  "op": "permission_reply",
  "request_id": "abc12",
  "behavior": "allow" | "deny"
}
```

**처리** (`server.ts:175-182`)
- server가 `notifications/claude/channel/permission` 알림으로 변환
- Claude의 permission callback이 호출됨

### signal
dispatcher → server, pane 관측 신호 (busy, idle, error 등).

```json
{
  "op": "signal",
  "session_id": "s1",
  "signal": "busy" | "idle" | "compact_error" | ...
}
```

## 거부된 도구 및 허용된 도구

dispatcher는 Claude Code 시작 시 도구 화이트리스트 설정:

**거부 도구** (`dispatcher.ts:46-51`)
```
mcp__plugin_telegram_telegram__reply
mcp__plugin_telegram_telegram__react
mcp__plugin_telegram_telegram__edit_message
mcp__plugin_telegram_telegram__download_attachment
```

(기존 plugin namespace는 차단)

**허용 도구** (`dispatcher.ts:52-57`)
```
mcp__tg_channel__reply
mcp__tg_channel__react
mcp__tg_channel__edit_message
mcp__tg_channel__download_attachment
```

(새 tg_channel namespace만 허용)

Claude Code는 `--disallowedTools` + `--allowedTools` 플래그로 실행 (`dispatcher-core.ts:44-45, 51-53`)

## 정크 메시지 및 충돌 감지

**Token collision** (`poller.ts:44-83`)
- Telegram API 409 Conflict 응답 감지
- 60초 슬라이딩 윈도우에서 3회 이상 충돌 시 anomaly log 및 경고 송신
- 다른 봇 인스턴스가 같은 token으로 폴링 중임을 의미

## Known Fragility

- 매우 큰 파일(50MB+) 첨부 시 일부 플랫폼에서 timeout 가능 (`server.ts:30`에 MAX_ATTACHMENT_BYTES 제한)
- token collision은 단순 횟수 감지이므로 일시적 네트워크 오류와 구분 어려움

## 참고

- 도구 호출 및 권한: [05-access-and-permissions.md](05-access-and-permissions.md)
- 구현 세부사항: `src/channels/telegram/server.ts`, `src/core/ipc.ts`
