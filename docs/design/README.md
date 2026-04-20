# claude-bridge 현재 기능 설계서 (AS-IS)

현재 코드 기준(2026-04-18, `main` 브랜치)으로 봇의 동작을 기술한다. 버그/개선 제안은 여기가 아니라 `docs/plans/` 와 `docs/reviews/` 에 기록한다.

## 읽는 순서

| # | 문서 | 한줄 요약 |
|---|------|----------|
| 01 | [architecture.md](01-architecture.md) | 런타임 토폴로지(Telegram ↔ bot.py ↔ tmux ↔ Claude Code)와 모듈 레이어링 |
| 02 | [streaming-pipeline.md](02-streaming-pipeline.md) | pane capture → parser → queue → sender 데이터 흐름, flush 종류 5가지 |
| 03 | [parser-and-boundaries.md](03-parser-and-boundaries.md) | 경계 감지 함수(`is_approval`, `_response_region`, …)와 `StreamQueue` 앵커 |
| 04 | [state-machine.md](04-state-machine.md) | `Bridge` 의 상태 플래그 전수 목록과 set/clear 경로 |
| 05 | [approval-flows.md](05-approval-flows.md) | 지속 승인 모드 vs bypass 모드 + resume/trust/ESC/늦은 승인 분기 |
| 06 | [observability.md](06-observability.md) | 로그 태그 체계, `dump/` 포맷, 버그 증거 수집 경로 |

## 규칙

- **AS-IS만** — 현재 코드 상태. 변경이 있으면 해당 문서를 갱신한다.
- **파일:라인 인용** — 본문 주장은 `bridge/core.py:438` 형태의 근거를 동반.
- **Known Fragility** — 각 문서 말미에 해당 영역에서 관측된 반복 회귀 패턴만 간단히 표기. 제안은 `docs/plans/` 로.
- **계층 위반 금지** — `bridge/__init__.py` 의 레이어 순서를 위반하는 기술은 버그로 취급.
