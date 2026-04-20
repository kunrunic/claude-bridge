# claude-bridge2 문서

Telegram ↔ Claude Code (tmux) 브리지 봇의 설계, 작업 계획, 리뷰 기록.

## 구성

| 폴더 | 목적 | 성격 |
|------|------|------|
| [design/](design/README.md) | **AS-IS 설계서** — 현재 코드가 어떻게 동작하는가 | 살아있는 문서 (코드 변경 시 갱신) |
| [plans/](plans/README.md) | **작업 계획** — 특정 변경의 의도·범위·diff·테스트 | 작업별 독립. 완료 후에도 보존 |
| [reviews/](reviews/README.md) | **리뷰 기록** — 다관점 교차 리뷰(아키텍트/시니어/전문가) | 작업별 독립. 계획의 전제 검증 |
| [ideas/](ideas/) | **아이디어 메모** — 정해지지 않은 구상 | 스케치. 구체화되면 plans/ 로 승격 |

## 작업 흐름

```
bugreport/  →  reviews/<date>-<topic>/  →  plans/<date>-<topic>/  →  코드 변경
(증상/로그)    (다관점 검토, 전제 검증)    (수정안, 테스트, 사이드이펙트)      (실제 적용)
```

- 버그 증상/로그는 `bugreport/<YYYYMMDD_HHMMSS>/` 에 보관 (루트 디렉토리)
- 수정 계획을 세우기 전에 `reviews/` 로 검토를 남긴다 — 계획이 잘못된 전제 위에 서는 걸 막음
- `plans/` 는 "무엇을, 왜, 어떻게" — Edit 단위 diff 와 테스트까지 포함
- AS-IS 설계(`design/`) 는 매 코드 변경 후 갱신 (PR 체크리스트에 포함)

## 네이밍 규칙

- 작업 폴더: `YYYYMMDD-kebab-topic` (예: `20260418-esc-flush-recovery`)
  - 같은 날 여러 작업: 타임스탬프 접미 (`20260418-1030-...`)
- design/ 파일: `NN-topic.md` — 순서가 의미 있음(읽는 순서 = 시스템 이해 순서)
- reviews/ 내부: 관점별 파일명 (`architect.md`, `senior-engineer.md`, `chat-streaming-expert.md`, 또는 `<role>_critic.md` / `<role>_positive.md`)

## 현재 작업

- **2026-04-20 MCP 채널 어댑터 전환** (Stage 4 완료, 본 저장소 현행 구현)
  - 계획: [plans/20260420-mcp-channel-adapter/](plans/20260420-mcp-channel-adapter/README.md)

## 이전 이력

Python pane-parsing 시대의 계획·리뷰·설계 문서는 태그 [`pre-python-removal-20260420`](https://github.com/kunrunic/claude-bridge/tree/pre-python-removal-20260420) 시점에서 조회할 수 있다. MCP 전환 이후 main 에서는 제거되었다.

## 규칙

- 문서는 **코드의 참(truth)을 반영** — 추측은 "Known Fragility" 등 전용 섹션에만.
- 파일 경로 + 라인 번호로 인용 (`parser.py:214-257`). grep 으로 찾을 수 있어야 한다.
- 날짜는 절대 날짜 (`2026-04-18`) 로. "어제", "다음주" 금지.
- 작업 폴더는 한 번 만들면 **삭제하지 않는다** — 과거 결정의 추적성 유지.
