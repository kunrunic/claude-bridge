# 06. 관측성 (Observability)

Always-on JSONL anomaly 로거, 구조화 로그 태그 체계, 테스트 레이아웃, CI 파이프라인.

## Anomaly 로거 (Always-On)

**파일**: `~/.claude-bridge/anomaly.jsonl`

각 라인은 JSON 레코드. 타임스탬프, anomaly kind, 컨텍스트 정보 포함.

### log() 함수

**위치** (anomaly.ts 에 구현)

```typescript
function log(kind: AnomalyKind, ctx: Record<string, unknown> = {}): void
```

**동작**:
1. `~/.claude-bridge/` 디렉토리 생성 (없으면)
2. rotateIfNeeded() — 파일 크기 체크
3. JSON 레코드 추가:
   ```json
   {
     "ts": "2026-04-20T10:30:45.123Z",
     "kind": "channel_reply_failed",
     "op": "dispatcher.announce",
     "error": "Network timeout"
   }
   ```

**설계**: 관측성 실패가 봇 로직을 중단하면 안 되므로 모든 예외를 silent catch (anomaly.ts 에 구현)

### AnomalyKind 열거

**정의** (anomaly.ts 에 정의)

| 종류 | 의미 |
|------|------|
| `channel_reply_failed` | Telegram API 송신 실패 |
| `permission_timeout` | 권한 요청 타임아웃 (현재 미사용) |
| `session_spawn_failed` | tmux new-session 또는 Claude Code 시작 실패 |
| `inbound_no_active_session` | 인바운드 메시지 but 활성 세션 없음 |
| `busy_timeout` | 세션이 너무 오래 busy (현재 미사용) |
| `compact_error` | Claude Code 압축 실패 |
| `mcp_unknown_method` | IPC 메시지 op 미인식 |
| `status_panel_edit_failed` | status 디스플레이 편집 실패 |
| `telegram_api_failed` | Telegram API 오류 (시작, 폴링 등) |
| `tmux_capture_failed` | tmux capture-pane 또는 send-keys 실패 |
| `token_collision_detected` | 같은 토큰으로 다른 인스턴스 폴링 |
| `anomaly_self_error` | 관측 시스템 자체 오류 |
| `shutdown` | 정상 종료 |
| `stale_instance_evicted` | 중복 인스턴스 제거 |
| `orphan_detected` | 부모 프로세스 사라짐 감지 |
| `server_startup` | MCP server 시작 (debugging info) |
| `ipc_connect_start` | IPC 연결 시도 |
| `ipc_connect_ok` | IPC 연결 성공 |
| `ipc_connect_failed` | IPC 연결 실패 |
| `ipc_hello_received` | hello 메시지 수신 |
| `rate_limit_hit` | Claude rate limit 감지 |

### 로그 로테이션

**rotateIfNeeded()** (anomaly.ts 에 구현)

파일 크기 5MB 초과 시:
```
anomaly.jsonl        → anomaly.jsonl.1
anomaly.jsonl.1      → anomaly.jsonl.2
anomaly.jsonl.2      → anomaly.jsonl.3
anomaly.jsonl.3 삭제 (KEEP=3)
```

최대 3개 파일 유지 (15MB 최대).

### summary()

**위치** (anomaly.ts 에 구현)

```typescript
function summary(windowMs: number): {
  total: number;
  byKind: Map<string, number>;
  lastTs: Map<string, string>;
}
```

**용도**: `/status` 커맨드에서 24시간 이내 anomaly 집계.

**윈도우**: windowMs만큼 이전 기록만 카운트
```typescript
const WINDOW_MS = 24 * 60 * 60 * 1000;
const s = anomaly.summary(WINDOW_MS);
```

**응답 포맷** (dispatcher.ts 에 구현)
```
📊 24h anomalies: 5
  channel_reply_failed: 2  (last 10:30:45)
⚠️ token_collision_detected: 1  (last 09:15:22)
log: /Users/user/.claude-bridge/anomaly.jsonl
```

## 로깅 위치별 정리

### Dispatcher

**anomaly 발생**:
- `channel_reply_failed` — 봇 공지 송신 실패
- `inbound_no_active_session` — 활성 세션 없을 때 inbound
- `mcp_unknown_method` — IPC op 미인식
- `shutdown` — graceful shutdown 로그
- `stale_instance_evicted` — PID lock에서 중복 제거

### MCP Server

**anomaly 발생**:
- `channel_reply_failed` — permission 알림 송신 실패
- `channel_reply_failed` — inbound 알림 송신 실패
- `mcp_unknown_method` — IPC op 미인식
- `anomaly_self_error` — IPC 파싱 오류
- `session_spawn_failed` — dev warning / trust dialog 타임아웃 또는 confirm 실패

### Poller

**anomaly 발생**:
- `telegram_api_failed` — bot.start() 폴링 오류
- `token_collision_detected` — 409 Conflict 반복
- `inbound_no_active_session` — permission reply 권한 부족

### IPC

**anomaly 발생**:
- `anomaly_self_error` — send() 오류
- `anomaly_self_error` — parse 오류

### Registry & Config

**anomaly 발생**:
- `anomaly_self_error` — sessions.findSessions 읽기 오류

### Tmux

**anomaly 발생**:
- `session_spawn_failed` — new-session 실패
- `tmux_capture_failed` — capture-pane 또는 send-keys 실패

### Lifecycle

**anomaly 발생**:
- `stale_instance_evicted` — 중복 인스턴스 강제 종료
- `orphan_detected` — 부모 프로세스 사라짐

## 신규 컴포넌트 문서화 (2026-04-21)

최근 리팩토링으로 핵심 기능이 분리됨:
- `SessionManager.ts` (199줄) — spawn / resume / fork / gracefulKill, dialog 자동 dismiss
- `TickObserver.ts` (85줄) — 5초 주기 pane 관찰, signal 변화 감지
- `IpcBridge.ts` (119줄) — MCP server ↔ dispatcher 자동 재연결 (지수 백오프)
- `SlashHandler.ts` (194줄) — Telegram 슬래시 커맨드 핸들러, InlineKeyboard 처리

이들 신규 파일은 dispatcher.ts 에서 조립되며, AS-IS 현황에 맞춘 설계서를 유지 중.

## 테스트 구조

**위치**: `tests/`

### Unit 테스트

- `unit/dispatcher-core.ts` — handleSlash, spawnSession, killSession 순수 함수
- `unit/slash.ts` — slash 파싱
- `unit/observer.ts` — pane signal 감지

**실행**:
```bash
bun test tests/unit/
```

### Integration 테스트

- `smoke-spawn.ts` — dispatcher 실행 → 세션 spawn → hello 메시지 확인
- `smoke-reply.ts` — reply 도구 호출 확인
- `smoke-permission.ts` — 권한 요청 흐름
- `smoke-mcp-stdio.ts` — MCP server stdio 통신

**실행**:
```bash
bun test tests/
```

### 배포 전 필수 체크

**happy-path smoke test** (`memory/MEMORY.md` 명시)

```bash
bun tests/smoke-spawn.ts
```

세션 spawn → "hello" 메시지 왕복 확인.

## CI 파이프라인

**파일**: `.github/workflows/ci.yml`

### 단계

1. **checkout** — 코드 다운로드
2. **setup** — Bun 설치
3. **install** — `bun install`
4. **typecheck** — `bun run typecheck` (TypeScript)
5. **test** — `bun test` (모든 테스트)
6. **smoke test** (선택) — 배포 전 smoke-spawn 실행

### 사양

- Node.js (via Bun) — 최신 버전
- timeout — 각 단계 30분 이내

## Anomaly 분석 도구

**로그 읽기**:
```bash
tail -f ~/.claude-bridge/anomaly.jsonl | jq '.'
```

**특정 kind 필터**:
```bash
cat ~/.claude-bridge/anomaly.jsonl | jq 'select(.kind == "token_collision_detected")'
```

**시간대별 필터**:
```bash
cat ~/.claude-bridge/anomaly.jsonl | jq 'select(.ts >= "2026-04-20T10:00:00")'
```

## 로그 파일 위치

| 파일 | 목적 | 로테이션 |
|------|------|---------|
| `~/.claude-bridge/anomaly.jsonl` | Always-on anomaly | 5MB / 3개 파일 |
| `~/.claude-bridge/inbox/YYYYMMDD/*.` | 첨부파일 | 자동 (날짜별) |
| `~/.claude/projects/*/session_id.jsonl` | Claude Code 세션 기록 | Claude Code 관리 |
| `~/.claude-bridge/dispatcher.sock` | IPC 소켓 (휘발성) | - |
| `~/.claude-bridge/telegram/bot.pid` | PID lock (임시) | - |

## 구조화 로그 스타일

모든 anomaly 레코드:
```json
{
  "ts": "ISO-8601",
  "kind": "AnomalyKind",
  // context-specific fields:
  "where": "function/module",
  "op": "operation name",
  "error": "error message",
  "sessionId": "s1",
  "chatId": "123",
  ...
}
```

**규칙**:
- `where` — 함수/모듈 위치 (예: "dispatcher.ts", "server.ipc")
- `op` — 수행 중이던 작업 (예: "permission_request_relay")
- `error` — Error.toString() 결과
- 모든 ID는 문자열 (chatId, userId, sessionId 등)

## 디버그 옵션

**CB_DUMP** 환경변수
- `CB_DUMP=1` — dumpEnabled 플래그 활성화
- 추가 상세 로그 기록 (현재 미사용, 향후 확장)

## Known Fragility

- rotateIfNeeded() 실패 시 silent catch이므로 로테이션 실패를 감지하기 어려움
- 매우 많은 anomaly가 발생하면 (초당 100+) 파일 쓰기 buffering 이슈 가능

## 참고

- 로거 구현: `src/core/anomaly.ts`
- IPC 프로토콜: `src/core/ipc.ts`
- CI 설정: `.github/workflows/ci.yml`
