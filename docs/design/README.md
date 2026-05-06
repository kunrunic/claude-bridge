# claude-bridge 현재 기능 설계서 (AS-IS)

채널 추상화 위에 동작하는 멀티 세션 Claude Code 브리지.
현재 Telegram, cb-menu (Ink TUI), tmux status bar 세 채널이 같은 dispatcher 위에서 공존한다.
현재 코드 기준 (`main` 브랜치) 으로 동작을 기술한다.
버그·개선 제안은 여기가 아니라 `docs/plans/` 와 `docs/reviews/` 에 기록한다.

## 읽는 순서

| # | 문서 | 한 줄 요약 |
|---|------|----------|
| 01 | [architecture.md](01-architecture.md) | 런타임 토폴로지 — Telegram, cb-menu, tmux status, MCP server 가 dispatcher 위에서 공존 |
| 02 | [mcp-channel-protocol.md](02-mcp-channel-protocol.md) | `bridge-channel` MCP 프로토콜 — reply / react / edit_message / download_attachment 도구 + inbound / permission 알림 |
| 03 | [session-lifecycle.md](03-session-lifecycle.md) | 세션 spawn / resume / fork / kill, workspace 격리, PID lock, orphan watchdog |
| 04 | [slash-commands.md](04-slash-commands.md) | Telegram 봇 명령어 — `/sessions`, `/new`, `/resume`, `/fork`, `/kill` |
| 05 | [access-and-permissions.md](05-access-and-permissions.md) | allowlist 게이트, 권한 요청 UI, 폴더 신뢰 자동 승인, 설정 권한 강화 |
| 06 | [observability.md](06-observability.md) | Always-on JSONL anomaly 로거, 구조화 로그, 테스트 레이아웃, CI 파이프라인 |
| 07 | [cli-channel.md](07-cli-channel.md) | cb 클라이언트 (SSH wrapper), cb-menu (Ink TUI 모드/F-key/RPC), TmuxStatusChannel (minimap renderer) |
| 08 | [channel-abstraction.md](08-channel-abstraction.md) | `Channel` 인터페이스, SessionEvent 종류, fan-out 패턴, 에러 계약 |

## 권장 읽기 경로

- **처음 보는 사람** — 01 → 08 (전체 그림) → 02 또는 07 (관심 채널)
- **Telegram 채널만 알고 싶다** — 01 → 02 → 04 → 05
- **cb CLI / 터미널 채널만 알고 싶다** — 01 → 07 → 03
- **새 채널 추가하려는 기여자** — 08 → 07 (TmuxStatusChannel 이 가장 단순한 예시)
- **세션 라이프사이클 디버깅** — 03 → 06

## 규칙

- **AS-IS만** — 현재 코드 상태. 변경이 있으면 해당 문서를 갱신한다.
- **파일:라인 인용** — 본문 주장은 `src/dispatcher.ts:280` 형태의 근거를 동반.
- **Known Fragility** — 각 문서 말미에 해당 영역에서 관측된 반복 회귀 패턴만 간단히 표기. 제안은 `docs/plans/` 로.
- **채널 무관성** — `src/core/*` 는 어떤 채널의 구현 상세도 알지 못한다. 채널 구현은 `src/channels/<name>/` 에만.
