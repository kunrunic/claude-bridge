# claude-bridge 현재 기능 설계서 (AS-IS)

MCP 채널 기반 Telegram ↔ Claude Code 멀티 세션 브리지. 현재 코드 기준(2026-04-21, `main` 브랜치)으로 봇의 동작을 기술한다. 버그/개선 제안은 여기가 아니라 `docs/plans/` 와 `docs/reviews/` 에 기록한다.

## 읽는 순서

| # | 문서 | 한줄 요약 |
|---|------|----------|
| 01 | [architecture.md](01-architecture.md) | 런타임 토폴로지(Telegram ↔ MCP stdio 서버 ↔ dispatcher ↔ tmux ↔ Claude Code)와 계층 분리 |
| 02 | [mcp-channel-protocol.md](02-mcp-channel-protocol.md) | Claude Code `claude/channel` MCP 프로토콜 (reply, react, edit_message, download_attachment 도구 + inbound/permission 알림) |
| 03 | [session-lifecycle.md](03-session-lifecycle.md) | 세션 spawn/resume/fork/kill, workspace 격리, PID lock, orphan watchdog |
| 04 | [slash-commands.md](04-slash-commands.md) | /sessions, /new, /resume, /fork, /kill, /current, /backlog, /status 명령어 |
| 05 | [access-and-permissions.md](05-access-and-permissions.md) | allowlist 게이트, 권한 요청 UI, 폴더 신뢰 자동 승인, 설정 권한 강화 |
| 06 | [observability.md](06-observability.md) | Always-on JSONL anomaly 로거, 구조화 로그, 테스트 레이아웃, CI 파이프라인 |

## 규칙

- **AS-IS만** — 현재 코드 상태. 변경이 있으면 해당 문서를 갱신한다.
- **파일:라인 인용** — 본문 주장은 `src/dispatcher.ts:380` 형태의 근거를 동반.
- **Known Fragility** — 각 문서 말미에 해당 영역에서 관측된 반복 회귀 패턴만 간단히 표기. 제안은 `docs/plans/` 로.
- **채널 무관성** — `src/core/*` 는 Telegram을 몰라야 함. `src/channels/telegram/*` 는 채널 구현만.
