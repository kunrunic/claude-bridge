# claude-bridge

Telegram 에서 내 PC 의 Claude Code 세션을 원격 조작하는 **MCP 채널 호스트**. 외출 중에도 자기 전 침대에서도 진행 중인 작업을 이어갈 수 있다.

Bun + TypeScript. Claude Code 의 `claude/channel` MCP 프로토콜 위에서 동작 — Claude 가 `reply()` / `react()` / `edit_message()` / `download_attachment()` 툴을 직접 호출해 대화한다.

---

## 구조

```
src/
  core/                          # 채널 무관 (공통 인프라)
    anomaly.ts                   # always-on JSONL 로거
    dispatcher-core.ts           # spawn / kill / handleSlash 순수 함수
    ipc.ts                       # dispatcher ↔ server 소켓 프로토콜
    lifecycle.ts                 # PID lock · orphan watchdog · shutdown
    observer.ts                  # Claude pane 상태 감지 (busy/compact/limit)
    registry.ts                  # 세션 레지스트리
    sessions.ts                  # ~/.claude/projects 스캐너 (/resume picker)
    slash.ts                     # /new /resume /fork /switch /kill …
    tmux/session.ts              # tmux new-session / send-keys / capture-pane
  channels/
    telegram/                    # 텔레그램 전용
      server.ts                  # MCP stdio 서버 (reply/react/edit/download)
      client.ts                  # grammy wrapper
      poller.ts                  # long-polling + permission callback
      access.ts                  # allowlist gate
      permissions.ts             # InlineKeyboard UI
      inbox.ts                   # 첨부파일 저장
      config.ts                  # 채널 설정 로드 + chmod
  dispatcher.ts                  # main entry (core + channel 조립)
tests/                           # Bun test (62개, 확장 중)
```

신규 채널 추가 시 `src/channels/<name>/` 하나 더 만들면 되도록 core/channel 경계 유지.

---

## 요구사항

- macOS / Linux
- [Bun](https://bun.sh) 1.2+
- tmux
- Claude Code CLI (`claude` 커맨드 PATH 에)
- Telegram Bot Token ([@BotFather](https://t.me/botfather))

## 설치

```bash
git clone <repo>
cd claude-bridge
bun install
```

설정 파일 `~/.claude-bridge/config.json`:

```json
{
  "botToken": "123:ABC...",
  "allowlist": ["YOUR_TELEGRAM_USER_ID"],
  "defaultChatId": "YOUR_TELEGRAM_USER_ID"
}
```

디렉토리/파일 권한은 기동 시 `0700` / `0600` 으로 자동 강화.

## 실행

```bash
bun run start           # 전경 (Ctrl+C 로 종료)
bun run dev             # --hot 모드
```

기동 시 Telegram 봇 메뉴(`/`)도 자동 등록.

## 명령어

| 커맨드 | 동작 |
|---|---|
| `/new [label] [cwd]` | 새 Claude 세션 spawn (컨텍스트 0) |
| `/resume [N\|id]` | 최근 세션 나열 / 동일 session-id 로 이어 실행 |
| `/fork [N\|id]` | 기존 세션 컨텍스트 상속 + 새 session-id |
| `/sessions` | 현재 활성 세션 목록 |
| `/switch <id\|label>` | 활성 세션 전환 |
| `/kill <id\|label>` | 세션 종료 |
| `/current` | 현재 활성 세션 |
| `/backlog [id\|label]` | 백그라운드 누락 메시지 열람 |
| `/status` | 24h anomaly 요약 |

## 개발

```bash
bun run typecheck       # tsc --noEmit
bun test                # 단위 + 통합 테스트
bun tests/smoke-spawn.ts   # tmux spawn → IPC hello 왕복 확인
```

`.git/hooks/pre-commit` 이 변경 포함 시 typecheck + test 자동 실행.

## 설계 문서

- 전략/설계: [`docs/plans/20260420-mcp-channel-adapter/`](docs/plans/20260420-mcp-channel-adapter/)
- Spike 관찰: [`docs/plans/20260420-mcp-channel-adapter/spike-log.md`](docs/plans/20260420-mcp-channel-adapter/spike-log.md)

## 이력

- 2026-04-20 Stage 4 컷오버 — Python pane-parsing bridge 전면 폐기, 본 TS 구현체로 교체
- 이전 Python 버전 롤백 태그: `pre-python-removal-20260420`, 로컬 백업: `~/bk_claude-bridge2_20260420/`

## 라이선스 · Attribution

MIT License — 자세한 내용은 [LICENSE](LICENSE) 참조.

MCP 채널 프로토콜 표면과 라이프사이클 패턴(PID lock / orphan watchdog 등)은
[anthropics/claude-plugins-official](https://github.com/anthropics/claude-plugins-official)
의 Telegram 플러그인(Apache 2.0) 을 레퍼런스로 삼아 독립 구현했다. 코드 사본은
포함되어 있지 않다.
