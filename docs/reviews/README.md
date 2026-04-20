# 리뷰 기록 (reviews/)

작업 계획을 확정하기 전에 **전제와 사이드이펙트**를 다관점으로 검증한 기록.

## 규칙

- 폴더명: `YYYYMMDD-kebab-topic` (plans/ 와 동일한 topic 을 쓰면 짝 맞음)
- 한 폴더 = 한 주제. 여러 관점 파일을 안에 둔다.
- 필수 파일:
  - `README.md` 또는 `SUMMARY.md` — 공통 합의, 관점별 차이, 권고 요지
  - 관점별 파일 — `architect.md`, `senior-engineer.md`, `chat-streaming-expert.md`, 또는 `<role>_critic.md` / `<role>_positive.md`

- 리뷰는 **계획 전**에 수행. 계획 폴더의 `side-effects.md` 에 리뷰 결과를 반영한다.
- 관점이 충돌하면 **충돌 그대로 기록**. 해결은 계획 단계에서.
- 에이전트로 생성했다면 모델명과 관점을 각 파일 상단에 명시.

## 왜 분리된 폴더인가

- `plans/` 는 "무엇을 어떻게" — 결정 사항.
- `reviews/` 는 "왜 이 결정인가" — 근거와 기각된 대안.
- 장래 회귀 발생 시 "그때 왜 이렇게 했지?" 에 답하는 자료.

## 현재 진행 / 완료

(MCP 체제 전환 후 신규 리뷰 미작성)

Python pane-parsing 시대의 리뷰 폴더들(20260415-initial-review, 20260418-esc-flush-recovery, 20260419-step-3-design)은 태그 `pre-python-removal-20260420` 시점에서 조회할 수 있다.

## 리뷰 수행 패턴 (이번 20260418 참고)

1. 대상 식별 — 어떤 코드 변경을 검토할지 특정
2. 관점 선정 — 최소 3관점 (architect / practitioner / domain-expert). 주제 따라 조정
3. 각 관점 에이전트에 개별 프롬프트 — 독립 사고 유지 (cross-contamination 방지)
4. 결과 수집 → 각 `<role>.md` 로 저장 (원문 유지, 요약 금지)
5. 공통 합의와 차이점을 `README.md` 에 요약 → plans 로 넘긴다

## 참고

- 상위: [../README.md](../README.md)
- 계획: [../plans/README.md](../plans/README.md)
- 설계: [../design/README.md](../design/README.md)
