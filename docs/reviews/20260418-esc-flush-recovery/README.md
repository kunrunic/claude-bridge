# 20260418 — ESC 이후 flush 봉쇄 회귀 3-에이전트 리뷰

## 배경

`bugreport/20260418_094744/BUG_REPORT.md` 의 제안 수정(Fix 1/2/3)을 수락하기 전에, 세 관점의 Opus 에이전트로 교차 리뷰를 돌려 본 작업의 사이드이펙트와 구조적 함의를 검증한 기록.

- 원 리포트: [`../../../bugreport/20260418_094744/BUG_REPORT.md`](../../../bugreport/20260418_094744/BUG_REPORT.md)
- 수정안 plan: [`../../plans/20260418-esc-flush-recovery/`](../../plans/20260418-esc-flush-recovery/README.md)

## 리뷰 구성

| 파일 | 관점 | 핵심 질문 |
|------|------|-----------|
| [architect.md](architect.md) | 소프트웨어 아키텍트 | "이 패턴의 회귀가 3연속 — 구조적 리빌딩이 필요한 지점인가?" |
| [senior-engineer.md](senior-engineer.md) | 시니어 개발자 (긍정·비판) | "제안 diff 의 사이드이펙트가 충분히 검토됐는가? 원 리포트가 놓친 케이스는?" |
| [chat-streaming-expert.md](chat-streaming-expert.md) | 채팅 스트리밍 전문가 | "앵커 기반 스트리밍이 근본 한계인가? bypass vs persistent-approval 플로우 분기를 어떻게 설계해야 하는가?" |

## 공통 합의 (3명 모두)

1. **Fix 1/2/3 은 즉시 반영해야 하는 올바른 핫픽스**. 구조 개선과 무관하게 회귀를 끊기 위해 먼저 머지.
2. **근본 원인은 경계 감지의 분산** — `is_approval` / `_response_region` / `is_trust_prompt` / `is_resume_picker` 가 각기 다른 tail 정책을 쓰고 있어, 한 곳만 고치면 다른 곳에서 재발.
3. **상태 머신 분산** — `awaiting_approval` 의 set/clear 가 6군데 이상에 흩어져 있어 이번 `cmd_esc` 누락처럼 새 진입점이 추가될 때마다 놓치기 쉬움.
4. **본 회귀 패턴은 3연속** — bc6a015, 4c20696, 20260418_094744. 같은 "scrollback-stale-marker-misread" 패턴이 경계를 옮겨가며 재발. 구조 개선 없이는 4번째를 막을 수 없다.

## 관점별 차이

| 항목 | architect | senior-engineer | chat-streaming-expert |
|------|-----------|-----------------|------------------------|
| Fix 1 의 tail 60 게이트 | 충분한 방어선 | 중첩 모달 케이스 회귀 테스트 필수 | `is_approval` 자체의 tail 정책과 일치시켜야 재발 방지 |
| 리빌딩 방향 | 옵션 A (Delta-only) 추천 | 헬퍼 도입(중간 단계) 선호 | Claude Code TUI 는 JSON 출력 없음 → 파서 유지 불가피 |
| 긴급성 | 중 (이번 핫픽스로 3-4 달 버팀) | 저 (테스트 강화 + 헬퍼로 충분) | 고 (파서 접근이 한계에 도달했다) |

## 본 작업 plan 에 반영된 권고

- Fix 1: `is_approval(text)` 게이트 **AND** tail 60 재스캔 (두 계층 방어선)
- Fix 2: `queue.reset()` 은 **추가하지 않음** — 시니어 리뷰의 race 분석이 "자연 복구" 를 확증
- Fix 3: 원안의 `not new_blocks and log_tag=="response"` 는 정상 케이스 스팸 → **`slip is not None` AND** 조건 추가
- 테스트: nested-modal + stale approval 두 케이스 반드시 커버

## 본 작업 범위 밖 (별도 추적)

아래 3건은 3명 모두 권고했으나 이번 세션에는 포함하지 않음:

1. **경계 감지 통합** — `boundary_from_live_box(text) -> int | None` 단일 함수
2. **상태 머신 일원화** — `Bridge.set_awaiting(context, ...)` / `clear_awaiting(reason)` 헬퍼
3. **Delta-only 스트리밍** — 아키텍트 옵션 A. 대공사이므로 별도 브랜치에서 POC 후 결정

근거와 세부 설계 스케치는 각 리뷰 파일 본문.

## 참고

- 수정 diff: [../../plans/20260418-esc-flush-recovery/fixes.md](../../plans/20260418-esc-flush-recovery/fixes.md)
- 사이드이펙트: [../../plans/20260418-esc-flush-recovery/side-effects.md](../../plans/20260418-esc-flush-recovery/side-effects.md)
- 테스트: [../../plans/20260418-esc-flush-recovery/test-plan.md](../../plans/20260418-esc-flush-recovery/test-plan.md)
- 이전 리뷰(2026-04-15): [../20260415-initial-review/SUMMARY.md](../20260415-initial-review/SUMMARY.md)
