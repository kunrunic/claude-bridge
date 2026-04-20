# Architecture Positive Review: Step 3 Design

## 단일 책임 원칙의 모범적 적용

**강점**: 6단계 파이프라인이 각 모듈을 명확한 계층으로 분리해 결합도를 최소화했다.

- **Tokenizer** → 바이트 → Token 변환 (ANSI 문법만 담당)
- **VTScreen** → Token → 24×200 그리드 상태 (VT100 에뮬레이션만)
- **LineCommitter** → 그리드 → LineCommit 판정 (row 확정 규칙만; 의미 해석 없음)
- **RegionTagger** → LineCommit → TaggedLine (box-drawing 위치 분석만)
- **EventClassifier** → TaggedLine → AnalyzerEvent (패턴 매칭 + FSM만)
- **BridgeAdapter** (신규) → AnalyzerEvent → Telegram (이벤트 소비 정책만)

각 모듈의 **public 인터페이스가 단일 dataclass** 로 고정된 점이 특히 우수하다:
- `Token` → LineCommit → `TaggedLine` → `AnalyzerEvent` 단방향 흐름
- 모듈 간 순환 import 0건 (§7 3a criterion 충족)

비교: 기존 `parser.py` 214-332 구간은 `_response_region` (경계 탐색) + `is_approval` (tail-scan) + `extract_response_blocks` (dedup) 이 **동일 함수 안에 뒤엉킨** 상태였다. 새 설계는 각 책임을 분리해 "새 이벤트 타입 추가 시 touch 모듈 ≤ 2개" (§7 3b) 를 달성했다.

## 관측성 & 재현성의 구조적 우위

**강점**: offset-based 이벤트 스트림이 버전 관리와 장애 대응을 획기적으로 개선한다.

기존 시스템:
- 완료 블록 (`⏺`) 을 매 tick 재파싱 → dedup 실패 시 침묵 (side-effects.md:123)
- 실패 케이스를 재현하려면 raw ANSI 를 수동으로 분석

신규 설계:
- `pipe-pane` append-only 파일 = 영구 기록. 손상/회전 감지도 inode 기반
- 모든 이벤트에 `offset` (byte 단위 session offset) 포함
- `--until-offset N` CLI 옵션으로 특정 지점까지의 상태를 **결정적으로 재현**
- shadow run 에서 `analyzer-events.jsonl` ↔ 기존 `events.jsonl` diff 자동 기록 (§8)

step-2-results.md 의 case-04 사례: 기존이 `is_approval: 0, queue_slip: 9` 로 침묵한 구간에서 분석기는 `approval_show(tool_hint="edit", summary="...") @ offset 451837` 을 명시 발화. **같은 offset 으로 `--until-offset 451838` 실행 → 그 직전까지 상태 재현 가능**. 현재 bridge 는 이런 사후 분석 경로가 전혀 없다.

## Paradigm 전환의 일관성

**강점**: "snapshot capture → 경계 탐색" 에서 "stream + event classification" 으로의 전환이 모순 없이 완결되어 있다.

step-3-design §3 스코프 매트릭스가 엄격함:
- 폐기: `_response_region` (VT row 가 직접 주어지므로 역방향 탐색 불필요)
- 폐기: `StreamQueue.idx/last_fp` dedup (offset 기반 structural dedup 로 상위 호환)
- 포팅: `_find_trust_prompt_region`, resume picker regex (도메인 지식)

"로직은 폐기, 도메인 지식은 채광" 원칙이 §1 에서 명확히 선언됐다. 이는 **두 paradigm 공존** (legacy 유지 vs 신규 통합) 이라는 함정을 피한다. 현재 pipe-pane logging (Step 1) 도 이미 기존 파서 경로와 독립적으로 장착돼 검증 중이다.

## 마이그레이션 경로의 안전성

**강점**: 4-α (shadow run) → 4-β (feature parity) → 4-γ (cutover) 3단계 분할로 롤백 안전성을 확보했다.

각 단계 가역성:
- Shadow run: 분석기를 병행 실행, dispatch 는 기존 parser 담당 → diff 기록만
- Feature parity: 분석기 이벤트 카탈로그 완성 → 테스트 추가만
- Cutover: `BRIDGE_DISPATCH_SOURCE=parser|analyzer` env 1개로 즉시 전환 가능

§5 step 4-γ 에서 "parser.py / stream_queue.py 는 남겨두되 비활성" 로 명시해, 회귀 시 LoC 손실 없이 재진입 가능. 현재 bridge 회귀 이력 (20260418, 20260417 × 3) 을 감안하면 이런 보수성이 가치 있다.

## 결론

아키텍처의 강점은 **단순성과 관측성의 조화**에 있다. 6개 레이어 각각이 단일 책임을 지고, 계층 간 의존이 명확하며, 마이그레이션 경로가 안전하다. "경계 탐색 복잡도 < event classification 복잡도" 가설이 실측으로 검증될 경우 (현재 PoC 단계), 이 설계는 대규모 리팩토링의 모범 케이스가 될 수 있다.
