# 05. 접근 제어 및 권한

Allowlist 게이트, 권한 요청 UI, 폴더 신뢰 자동 승인, 설정 파일 권한 강화.

## Allowlist 게이트

**gate()** (access.ts 에 구현)

Telegram inbound 메시지 필터링:

```typescript
function gate(config: Config, chatId: string, userId: string): AccessDecision
```

**규칙** (순서대로):
1. allowlist 비어있으면? → `drop` ("allowlist empty")
2. userId가 allowlist에 있으면? → `allow`
3. chatId가 allowlist에 있으면? → `allow`
4. 그 외 → `drop` ("user_id ... not in allowlist")

**사용 위치** (poller.ts 에서 호출)
```typescript
const decision = gate(this.config, chatId, userId);
if (decision.action === "drop") {
  anomaly.log("inbound_no_active_session", {
    chatId, userId, reason: decision.reason, ...
  });
  return;
}
```

**Allowlist 설정** (config.ts 에 정의)
```json
{
  "allowlist": ["user_id_1", "chat_id_1", "user_id_2"]
}
```

- user_id: 개인 DM 허용
- chat_id: 그룹 채팅 허용
- 혼합 가능

**특수 항목**: `defaultChatId` (선택사항)
- 봇 내부 announcements 용 (세션 없을 때 경고 등)

## 권한 요청 (Tool Permission)

Claude Code에서 도구(예: `reply`) 호출 시 권한 필요. MCP permission system 사용.

### 흐름

1. **Claude가 도구 호출 요청**
   - `reply` 등 도구 사용

2. **MCP server permission notification**
   - `notifications/claude/channel/permission_request` 알림 발송
   - 도구명, 설명, input preview 포함

3. **dispatcher 중계** (IPC 모드)
   - server → dispatcher로 `permission_request` IPC 메시지
   - dispatcher가 allowlist의 모든 chat_id에 Telegram InlineKeyboard 메시지 전송

4. **사용자 승인/거절**
   - Telegram에서 ✅/❌ 버튼 클릭
   - `handleCallback()` 또는 `handleInbound()` (REPLY_RE match) 처리

5. **poller에서 permission 콜백 호출**
   - `onPermissionReply(requestId, "allow"|"deny")`

6. **dispatcher → server permission_reply**
   - dispatcher가 server에 `permission_reply` IPC 메시지 전송
   - server가 `notifications/claude/channel/permission` 알림으로 변환

7. **Claude 계속 실행**
   - permission callback 반환

### 권한 요청 UI

**Compact 형식** (permissions.ts 에 정의)
```
🔐 Permission: mcp__bridge-channel__reply
[See more] [✅ Allow] [❌ Deny]
```

- "See more" 버튼 클릭 → expanded 형식 전환

**Expanded 형식** (permissions.ts 에 정의)
```
🔐 Permission: mcp__bridge-channel__reply

tool_name: mcp__bridge-channel__reply
description: Send a message to Telegram
input_preview:
{
  "chat_id": "123",
  "text": "hello world"
}

[✅ Allow] [❌ Deny]
```

**InlineKeyboard 구성** (permissions.ts 에 정의)
- Compact: 3개 버튼 (See more, Allow, Deny) — 1행
- Expanded: 2개 버튼 (Allow, Deny) — 1행

### Callback 형식

**CALLBACK_RE** (permissions.ts 에 정의)
```
^perm:(allow|deny|more):([a-km-z]{5})$
```

예: `perm:allow:abc12`, `perm:deny:abc12`, `perm:more:abc12`

**REPLY_RE** (permissions.ts 에 정의)
```
^\s*(y|yes|n|no)\s+([a-km-z]{5})\s*$
```

텍스트 기반 응답: `yes abc12`, `no abc12` (대소문자 무관)

### 권한 저장소

**pendingPermissions** (permissions.ts 에 정의)
```typescript
const pendingPermissions = new Map<string, PermissionDetails>();
```

request_id → {tool_name, description, input_preview}

권한 해결 후 삭제

### 승인 시 처리

**Callback 핸들러** (poller.ts 에 구현)

1. 데이터 파싱: `CALLBACK_RE.exec(data)`
2. 인증: sender가 allowlist에 있는가?
3. behavior = "allow" or "deny"
4. `onPermissionReply(requestId, behavior)` 호출
5. Telegram 메시지 편집: "✅ Allowed" 또는 "❌ Denied" 추가

**Inbound 텍스트 매칭** (poller.ts 에 구현)

REPLY_RE 매칭: "yes abc12" 또는 "no abc12"
- 같은 처리 흐름

## 폴더 신뢰 UI 자동 확인

Claude Code 실행 시 "Trust this folder?" 프롬프트 가능.

dispatcher가 자동으로 처리:

**confirmTrustDialog()** (dispatcher.ts 에 구현)
- 10초 폴링 (200ms 간격)
- "I trust this folder" 마커 감지
- 감지되면 Enter 키 전송

## 설정 파일 권한 강화

**loadConfig()** (config.ts 에 구현)

```typescript
function hardenPermissions(path: string): void {
  const dir = dirname(path);
  const dirMode = statSync(dir).mode & 0o777;
  if (dirMode !== 0o700) chmodSync(dir, 0o700);
  const fileMode = statSync(path).mode & 0o777;
  if (fileMode !== 0o600) chmodSync(path, 0o600);
}
```

**적용**:
- `~/.claude-bridge/config.json` 파일: 0600 (소유자만 읽기/쓰기)
- `~/.claude-bridge/` 디렉토리: 0700 (소유자만 접근)

**시점**: config 로드 시 자동 실행

## 설정 스키마

**Config** (config.ts 에 정의)

```json
{
  "botToken": "string (required)",
  "allowlist": ["string array (default [])"],
  "defaultChatId": "string (optional)",
  "dumpEnabled": "boolean (default false)"
}
```

**환경변수 오버라이드** (config.ts 에 구현)
- `TELEGRAM_BOT_TOKEN` → botToken 우선
- `CB_DUMP` → dumpEnabled 우선

## 권한 요청 스탠드얼론 모드

dispatcher 없이 server만 실행되는 경우:

**permission_request 처리** (server.ts 에 구현)
- 직접 allowlist의 모든 chat_id에 Telegram 메시지 전송
- dispatcher를 거치지 않음

이 모드는 개발/테스트 용도.

## Allowlist 확인 헬퍼

**assertAllowedChat()** (access.ts 에 구현)

```typescript
function assertAllowedChat(config: Config, chatId: string): void {
  if (!config.allowlist.includes(chatId) && config.defaultChatId !== chatId) {
    throw new Error(`chat_id ${chatId} not in allowlist`);
  }
}
```

특정 API 호출 전 사용 (실제 코드에서는 gate() 사용이 우선)

## Permission Timeout 및 GC

**pendingPermissions**는 요청 해결 후 즉시 삭제.

매우 오래된 권한 요청(예: 1시간 이상)은 UI에서는 사라지지만, 정확한 timeout 정책은 없음 (권장: 권한 요청 빠른 응답).

## Token Collision 감지 및 경고

권한 요청 중 Telegram API 409 Conflict 감지 시:
- 같은 토큰으로 다른 봇 인스턴스 폴링 중
- `token_collision_detected` anomaly log
- allowlist의 모든 chat_id에 경고 메시지 전송

## Known Fragility

- Allowlist가 비어있으면 모든 inbound를 거절. 초기 설정 필수.
- 권한 요청이 매우 많으면 (동시에 100개 이상) Telegram API rate limit 가능
- 폴더 신뢰 UI 자동 확인이 10초 타임아웃이므로 매우 느린 경우 놓칠 수 있음

## 참고

- 설정 로드: `src/channels/telegram/config.ts`
- 게이트: `src/channels/telegram/access.ts`
- 권한 UI: `src/channels/telegram/permissions.ts`
- Poller 처리: src/channels/telegram/poller.ts 에 구현
