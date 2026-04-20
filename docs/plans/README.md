# 작업 계획 (plans/)

특정 변경의 의도, 범위, diff, 테스트, 사이드이펙트를 한 폴더에 담는다.

## 규칙

- 폴더명: `YYYYMMDD-kebab-topic` (같은 날 여러 작업이면 `YYYYMMDD-HHMM-topic`)
- 한 폴더 = 한 브랜치/한 PR 단위. 여러 커밋을 묶어도 되지만 주제는 단일.
- 필수 파일:
  - `README.md` — 배경, 원인 요약, 변경 파일 범위, 체크리스트
  - `fixes.md` — 실제 diff 와 의도 (Read + Edit 타겟을 그대로 재현 가능하도록)
  - `test-plan.md` — 추가할 테스트, 회귀 확인 범위, 실행 게이트
  - `side-effects.md` — 리스크 표, 대응, 기각된 대안 (리뷰가 있다면 그 결과 반영)

- **계획은 실행 전에 쓴다**. 사후 정리 문서로 쓰면 scope creep 과 "나중에 다시 하자" 의 저장소가 됨.
- 선행 리뷰가 있으면 `reviews/<same-topic>/` 링크를 README 상단에 박는다.
- 계획은 **취소/변경되어도 폴더를 삭제하지 않는다** — 왜 변경됐는지의 추적성이 더 중요.

## 작성 흐름

1. `bugreport/<YYYYMMDD_HHMMSS>/BUG_REPORT.md` 정독
2. 필요 시 `reviews/<date>-<topic>/` 로 다관점 검토 (3-에이전트 등)
3. `plans/<date>-<topic>/README.md` 골격 작성 — 원인 가설 + 변경 파일 + 체크리스트
4. `fixes.md` 에 diff 상세. Edit 도구로 실제 적용할 수 있을 수준.
5. `test-plan.md` 에 신규 테스트 + 기존 회귀 매트릭스
6. `side-effects.md` 에 리뷰 결과 반영 + 기각된 대안 기록
7. 실제 적용 → pytest 통과 → 커밋 메시지 초안 제시 (커밋은 사용자 지시 전까지 금지)

## 현재 진행 / 완료

| 폴더 | 상태 | 요지 |
|------|------|------|
| [20260419-pipe-pane-redesign-poc/](20260419-pipe-pane-redesign-poc/README.md) | 설계 | pipe-pane 생로그 + 이벤트 기반 스트리밍 재설계 PoC. Step 3 결정 게이트 |
| [20260418-esc-flush-recovery/](20260418-esc-flush-recovery/README.md) | 진행 | ESC 이후 Telegram flush 봉쇄 회귀. Fix 1/2/3 핫픽스 |
| [20260415-initial-plan/plan.md](20260415-initial-plan/plan.md) | 완료 | 초기 설계 리뷰 반영 개선 계획 |

## 참고

- 상위: [../README.md](../README.md)
- AS-IS 설계: [../design/README.md](../design/README.md)
- 리뷰: [../reviews/README.md](../reviews/README.md)
