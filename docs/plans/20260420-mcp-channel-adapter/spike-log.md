# 단계 0 Spike — 공식 Telegram 플러그인 관찰

실행일: 2026-04-20
관찰자: Claude (session 828168cb), 실제 유저 테스트 병행 필요

## 1. 환경 스냅샷

본 spike 는 "설치부터"가 아니라 **플러그인이 이미 활성 상태**라는 전제에서
시작함 (유저가 사전에 세팅).

### 1.1 실행 중인 프로세스

```
PID 40294  /Users/kunrunic/.local/bin/claude --resume a308d08b...
           --channels plugin:telegram@claude-plugins-official
PID 40349  bun run --cwd .../telegram/0.0.6 start
PID 40352  /Users/kunrunic/.bun/bin/bun server.ts        ← MCP server
```

즉 Claude Code 1개 세션이 플러그인을 붙여서 돌고 있고, MCP 서버가 grandchild 로
스폰됨 (계층: claude → bun wrapper → server.ts).

### 1.2 파일 시스템 상태

```
~/.claude/channels/telegram/
  ├─ .env              (TELEGRAM_BOT_TOKEN, 0600 — chmod 됨)
  ├─ access.json       (dmPolicy=allowlist, allowFrom=["898522871"])
  ├─ approved/         (빈 디렉토리)
  ├─ bot.pid           (40352 — 현재 MCP 서버 PID)
  └─ inbox/ (미생성)   (첨부 도착 시 생성)
```

- 플러그인 버전: **0.0.6** (`installed_plugins.json`)
- 토큰 **별도** 사용 확인: 플러그인 `8565...` vs claude-bridge2 `8711...` →
  polling 충돌 없음. 현재 1봇-1세션 구성.
- 페어링은 이미 완료 (`dmPolicy=allowlist`).

### 1.3 동시 실행 중인 claude-bridge2

```
PID 34813  python -u bot.py claude-bridge2       (기존 브릿지)
PID 21095  tmux new-session -d -s claude_bridge   (기존 tmux Claude)
```

→ **기존 브릿지는 무영향**. 다른 봇/다른 tmux 세션/다른 Claude 프로세스.

---

## 2. server.ts 에서 확정한 동작 사실

전체 1032 줄 중 필요 부분 직접 조회 (gh api). 설계 §2 를 실제 코드로 검증.

### 2.1 인바운드 처리 흐름 (`handleInbound` 진입 전)

```
Telegram → grammy long-poll → bot.on('message:text'|':photo')
  → gate(ctx):
       dmPolicy == 'disabled'        → drop
       private AND allowFrom 포함    → deliver
       private AND pairing 모드       → 6자리 hex 코드 발급 (신규), 재송신 (기존)
       group/supergroup               → allowFrom + requireMention 체크
  → deliver 시 handleInbound() 호출
```

### 2.2 `reply` 툴 (outbound)

**요약**: text 먼저 chunk 분할 전송 → `files[]` 는 별도 메시지로 전송 (Telegram
은 text+file 을 한 sendMessage 에 담지 못함).

- 텍스트 chunk 한도: 기본 4096 (Telegram 하드 캡), `textChunkLimit` 로 조정 가능.
- 분할 모드: `length` (단순 cut) / `newline` (문단 경계 우선).
- `reply_to` + `replyToMode`:
  - `off`: 절대 스레드 안 함
  - `first` (기본): 첫 chunk 에만 `reply_parameters`
  - `all`: 모든 chunk 에 `reply_parameters`
- `format`: `'text'` (기본) / `'markdownv2'`. MarkdownV2 는 호출자가 직접
  escape 책임.
- 첨부 이미지: `.jpg .jpeg .png .gif .webp` → `sendPhoto`. 그 외 → `sendDocument`.
- 50MB 초과 거부. `STATE_DIR` 내부 (access.json, .env 등) 는 `assertSendable`
  로 절대 송신 불가 (state 유출 방지).

### 2.3 `permission_request` relay

```
Claude → MCP 서버:
  notifications/claude/channel/permission_request
    { request_id, tool_name, description, input_preview }

서버 → Telegram:
  sendMessage(chat_id, "🔐 Permission: <tool_name>",
              reply_markup = InlineKeyboard [
                 "See more" → callback perm:more:<id>
                 "✅ Allow"  → callback perm:allow:<id>
                 "❌ Deny"   → callback perm:deny:<id>
              ])

유저 → Telegram 버튼 탭:
  bot.on('callback_query:data') 진입:
    - allowFrom 재확인 (authz)
    - 'more' 는 input_preview 를 pretty-JSON 으로 확장, 새 버튼 2개
    - 'allow'/'deny' 는 mcp.notification({
          method: 'notifications/claude/channel/permission',
          params: { request_id, behavior: 'allow'|'deny' }
      })
    - 메시지 텍스트에 "✅ Allowed"/"❌ Denied" 추가, 버튼 제거 (재응답 방지)
```

**결론**: 승인 UX 는 완전히 서버사이드에서 처리 → 우리 bridge 가 파싱/추적할
부분 없음. 타임아웃 처리는 **서버에도 없음** — Claude Code 쪽에서 대기. 이건
우리가 자체 구현 시 `permission_timeout` 을 anomaly 로 기록해야 할 이유.

### 2.4 `download_attachment`

유저가 사진 보내면 gate 통과 후 `INBOX_DIR` 에 즉시 다운로드되고, 인바운드
블록 내 `attachment_file_id` meta 가 Claude 쪽에 전달됨. Claude 가 `Read` 하고
싶으면 먼저 `download_attachment(file_id)` 를 호출 → 로컬 경로 반환.

```
path = join(INBOX_DIR, `${Date.now()}-${uniqueId}.${ext}`)
```

### 2.5 `edit_message`

- 기존 봇 메시지 편집.
- **푸시 알림 없음** (instructions 에 명시): "when a long task completes, send
  a new reply so the user's device pings."
- 즉 상태판 pinned edit 전략은 실현 가능.

### 2.6 라이프사이클 / 충돌 방지

```
bun → server.ts 기동:
  1. .env chmod 0600 + process.env 로 로드 (기존 env 가 있으면 보존)
  2. STATE_DIR mkdir 0700
  3. bot.pid 파일 읽음 → stale PID 있으면 SIGTERM
  4. writeFileSync(PID_FILE, getpid)
  5. grammy Bot 기동, mcp.connect(new StdioServerTransport())

종료 (stdin EOF / SIGTERM / SIGINT / SIGHUP / orphan watchdog 5초 poll):
  1. PID_FILE 제거 (본인 PID 일 때만)
  2. bot.stop() (long-poll timeout 까지 최대 몇 초)
  3. 2초 뒤 process.exit(0) 강제
```

Orphan watchdog (`bootPpid` 비교 + `stdin.destroyed` 체크, 5초 폴) 덕분에 Claude
Code 가 크래시해도 zombie MCP 남지 않음. **우리 자체 구현도 같은 패턴 채택 필요**.

---

## 3. 설계 §9 미해결 항목 — spike 로 답할 수 있는 것

| 질문 | 답 | 근거 |
|---|---|---|
| `reply()` 가 실제 도착하는가 | ✅ chunk 분할 + sendMessage (4096 한도 준수) | server.ts L513-574 |
| `permission_request` UX | InlineKeyboard 3버튼 (See more / Allow / Deny) | L670-740 |
| `--dangerously-skip-permissions` 조합 | 로컬 툴과 permission_request 레이어 **독립** | Claude Code 문서 + plugin 에 해당 체크 로직 없음 |
| `<channel>` 블록 형식 | `<channel source="telegram" chat_id="..." message_id="..." user="..." ts="..." [image_path=...] [attachment_file_id=...]>` | instructions 블록 (server.ts L216) |
| 세션 resume | Claude Code 본인 `--resume` 이 처리. 플러그인은 stateless, access.json 만 재활용 | - |
| 이미지 왕복 | 인바운드: INBOX_DIR 자동 저장 / 아웃바운드: `reply` 의 `files[]` 로 경로 첨부 | L773-790 (photo), reply 툴 내부 |
| edit_message 푸시 | **없음** (instructions 명시) | server.ts L216 |
| 토큰 충돌 | stale PID SIGTERM + orphan watchdog → 동일 토큰 2세션 병행 불가 | L65-72 |
| STATE_DIR 분리 | `TELEGRAM_STATE_DIR` env 로 가능, `.env` 각자 보유 | L28 |
| group 승인 relay | **의도적 미지원** (보안 결정) | L624 코멘트 |

### 남은 질문 (실제 사용 체감 필요 — 유저 몫)

| 질문 | 왜 유저만 답할 수 있나 |
|---|---|
| 긴 작업 중 "Claude 가 말 안 하면 답답한 정도" | 주관적 UX |
| reply() 의 문체가 우리가 원하는 톤인가 | Claude 응답 성향 관찰 |
| 스레드 (`reply_to`) 가 실제 앱에서 보기 좋은지 | Telegram 클라이언트별 렌더링 |
| 한국어 Markdown/MarkdownV2 escape 실수 빈도 | 실제 전송 테스트 필요 |

---

## 4. 유저 테스트 시나리오 (필요 시)

현재 플러그인이 이미 PID 40294 Claude 세션에서 동작 중. 아래 시나리오를 해당
세션에서 돌리면서 관찰 기록을 이 섹션에 추가:

### 4.1 단순 메시지

- [ ] Telegram 에서 "git status 보여줘" 전송
- [ ] Claude 가 Bash 실행 후 `reply()` 호출하는지 확인
- [ ] Telegram 에 어떤 메시지가 도착했는지 원문 기록
- [ ] 결과를 그대로 붙이는지, 요약해서 주는지 관찰

**관찰 기록**:
```
(유저가 작성)
```

### 4.2 이미지 첨부

- [ ] Telegram 에서 사진 + "이 이미지 뭐야?" 전송
- [ ] `~/.claude/channels/telegram/inbox/` 에 파일 생성 확인 (`ls -la`)
- [ ] Claude 가 `download_attachment` 혹은 직접 `Read` 하는지 관찰
- [ ] 응답 도착까지 소요 시간

**관찰 기록**:
```
(유저가 작성)
```

### 4.3 승인 박스

- [ ] `--dangerously-skip-permissions` 없이 세션 재시작 (spike 테스트 전용으로)
- [ ] Telegram 에서 "README.md 한 줄 고쳐줘" 같이 쓰기 권한 필요한 요청
- [ ] 🔐 Permission 메시지 도착 시 UI 스크린샷
- [ ] "See more" 탭 → 어떤 정보가 확장되는지
- [ ] "✅ Allow" 탭 → 메시지가 어떻게 변하는지 (버튼 제거 / "✅ Allowed" 추가 여부)

**관찰 기록**:
```
(유저가 작성)
```

### 4.4 긴 작업 침묵 대응

- [ ] "tests 디렉토리 전부 훑어보고 문제점 정리해줘" 같은 2~3분 걸리는 요청
- [ ] Claude 가 중간에 `edit_message` 나 `reply` 로 progress 보고하는지
- [ ] 중간 보고 안 하면: `edit_message` 를 쓰도록 명시적으로 요청하면 반영되는지
- [ ] 우리가 설계에서 가정한 "busy→idle 감지 시 알림 카드" 대체제가 필요한지
  판단

**관찰 기록**:
```
(유저가 작성)
```

---

## 5. 결론 — 자체 구현 범위 확정

위 관찰로부터 `docs/plans/20260420-mcp-channel-adapter/design.md` 의 범위가
정당한지 재확인:

| 우리가 자체 구현해야 할 부분 | 이유 |
|---|---|
| Telegram polling 단일 진입점 | 토큰 1개 = polling 1개 제약. 멀티세션 디스패처 위해 필수 |
| chat_id → 활성 세션 라우팅 | 공식 플러그인은 1봇-1세션 고정 |
| 세션별 MCP stdio 서버 | 공식 서버 코드 구조를 레퍼런스로 Python 포팅 |
| `permission_timeout` / `channel_reply_failed` anomaly | 공식 서버에는 없음 — 우리의 운영 요구 |
| 멀티세션 UX (slash 커맨드, 알림 카드, 상태판) | 공식 플러그인은 의도적 미지원 |

| 공식 동작을 **그대로 미러링**할 부분 | 이유 |
|---|---|
| `reply` / `react` / `edit_message` / `download_attachment` 툴 시그니처 | 스펙 호환성 |
| `notifications/claude/channel/permission_request` 수신 규격 | Claude Code 가 주체 |
| `notifications/claude/channel/permission` 응답 규격 | 동 |
| `<channel>` 인바운드 블록 포맷 | 동 |
| `STATE_DIR` 레이아웃 (access.json / approved/ / inbox/ / bot.pid) | 기존 skill 재사용 가능성 |
| PID 충돌 + orphan watchdog 패턴 | zombie 방지 |

---

## 6. 다음 액션

1. 유저가 §4 시나리오 1개 이상 실행하고 "관찰 기록" 채움
2. 채운 결과 기반으로 `design.md` §9 업데이트 (예: MarkdownV2 escape 정책, 긴
   작업 침묵 대응 정책)
3. 단계 1 착수: `bridge/mcp_channel/` 스켈레톤 작성 (Python MCP SDK)

### 오픈 질문 (유저 확인 필요)

1. 단계 1 Python MCP 서버를 `bridge/` 하위에 통합할까, 별도 패키지
   (`mcp_channel/`) 로 뽑아서 pip install 가능하게 할까?
2. 기존 claude-bridge2 와 병존 기간 — 새 채널이 stable 이라고 판단하기까지 어느
   정도 병렬 운영할지?
3. 테스트 봇을 spike 용으로 계속 유지할지, 마이그레이션 완료 후 제거할지?
