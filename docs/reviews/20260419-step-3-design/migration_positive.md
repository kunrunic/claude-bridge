# Migration 강점 — Step 3 설계 평가

## 요약

Step 3 의 전환 전략은 **단계적 가역성** 과 **실측 검증** 을 기본축으로 설계되어, 기존 parser.py 를 안전하게 대체할 수 있는 기초를 갖추고 있다. 특히 shadow run 기법과 rollback flag 는 본 설계의 가장 강력한 요소다.

---

## 1. 가역적 단계 분할 (Step 4-α/β/γ)

**강점**: 각 단계가 환경변수 하나로 롤백 가능하도록 설계됨.

- **Step 4-α (Shadow Run)**: 분석기를 병행 실행하되 dispatch 는 여전히 parser 경로 담당. Telegram 사용자는 영향받지 않음.
  - 실패 시 즉시 중단 → 기존 path 그대로 운영.
  - 비용: 24-72h 관찰 기간, 시스템 리소스 2배 사용.

- **Step 4-γ (Adapter Cutover)**: `BRIDGE_DISPATCH_SOURCE=parser|analyzer` env 플래그로 진짜 전환점.
  - 문제 발생 시 env 하나 변경 → 프로덕션 1주 내 즉시 원상복구.
  - 기존 parser.py / stream_queue.py 는 비활성 상태로 남아있어 frozen 가능.

**근거**: 설계 §5 Step 4-γ / §9 exit criteria.

---

## 2. 정량적 판정 기준 (§5 shadow run criteria)

**강점**: 명확한 수치 기준으로 "통과/실패" 구분.

- `block_commit` 개수 ±5% 이내 (기존 `response_block` 대비).
- `approval_show` 발생이 기존 `is_approval=1` dump 의 **superset** (기존이 못 본 케이스는 OK, 반대는 NG).
- 스피너 rate ≤ 5건/초 (현재 실측 0.07건/초로 여유충분).

→ "마음의 준비 안 됨" 주관식 판정 가능성 제거. 수치 실패는 재작업, 수치 통과면 진행.

**근거**: step-2-results.md 의 criterion 검증 + 설계 §5 Step 4-α.

---

## 3. 도메인 지식 채광 계획 (§1)

**강점**: "로직 폐기, 지식 채광" 의 경계를 명확히 함.

- **버려지는 로직**: `_response_region` 경계 탐색 (VT row 가 직접 주어지므로 불필요), `StreamQueue.idx/last_fp` dedup (offset+timestamp 로 구조적 해결).
- **채광되는 지식**: 
  - UI 패턴 카탈로그 (승인 박스의 3단 구조, divider 부재 체크 등) → region_tagger + event_classifier 에 regex 이관.
  - edge case 경험 (좁은 터미널, nested modal, scrollback eviction) → 테스트 fixture + synthetic case 로 보존.
  - 문자열 fingerprint (`_fp`, last_fp 앵커 개념) → analyzer 의 offset-based dedup 에 흡수.

→ 대체 불가능한 "사용자 경험 누적" 은 손실 없음. 단순 boundary 탐색 로직만 구조적 대체.

**근거**: 설계 §2 discard policy, step-2-results.md 의 case-04 실증 (분석기가 parser 를 못 본 사건을 포착).

---

## 4. 실측 증거 (Step 2 PoC)

**강점**: 설계가 실제 작동하는 분석기 오프라인 구현에 기반함.

- case-04: parser 는 `is_approval=0` (침묵), 분석기는 `approval_show` 1건 emit → **기존보다 정확**.
- 243 + 53 신규 테스트 passing → 레그레션 기초선 확보.
- offset-based replay (`--until-offset N`) → 장애 재현 결정성 보장.

→ "미래 불확실성" 이 아닌 "현재 실제 구현" 을 검증. 과신이지만 과신할 만한 근거 있음.

**근거**: step-2-results.md 전체, 설계 §8 관측성.

---

## 5. 모듈 경계의 명확성 (§3 TO-BE)

**강점**: 각 단계가 순수 함수 + dataclass 입출력으로 고정.

```
Tokenizer → VTScreen → LineCommit → RegionTagger → EventClassifier → BridgeAdapter
```

- 각 모듈의 교체 또는 수정 시 **인접 모듈만** 영향 (§3a criterion).
- 신규 이벤트 추가 시 EventClassifier + Test 만 터치 (§3b criterion).
- 각 모듈이 의존성 없이 단위테스트 가능 (§3c criterion).

→ "parser.py 전체 518 LoC 를 한 덩어리로 대체" 보다 훨씬 유지보수 안전.

**근거**: 설계 §7 Decision Criterion 3' (LoC 대신 3축 측정).

---

## 6. Frozen Parser 통제 (§10 Q3 권장안)

**강점**: Step 4-γ ~ Step 5 사이 parser.py 를 수정금지 영역으로 동결하면, "두 paradigm 공존" 의 유지보수 부채를 최소화.

- 기간: 대략 1주 (shadow 72h + cutover 1주) 정도로 명시.
- 정당성: 그 동안 새 parser 버그는 차라리 분석기 쪽 이벤트 누락으로 나타나 → shadow run diff 에서 포착 가능.
- 비용: 일시적 (Step 5 에서 제거).

→ "2배 maintenance" 논거의 실제 구현화. 단기 고통으로 중기 안정성 확보.

**근거**: 설계 §1 "2배 maintenance" 문제, §10 Q3 권장.

---

## 7. 관측성 표준의 일관성 (§8)

**강점**: dump.events() 를 단일 포맷으로 통일.

- 모든 이벤트: `{offset, t, region, payload}` 포맷 (분석기 / bridge / suppress).
- shadow run 동안 diff 자동 기록 (`~/.claude-bridge/panes/<session>/shadow-diff.jsonl`).
- 프로덕션 후 "무회귀" 판정 시 events.jsonl 만 보면 됨 (§9 Step 5 exit criterion).

→ "조용한 회귀" 가능성이 낮음. 이벤트 누락 / 오탐은 diff 에 자동 노출.

**근거**: 설계 §8, step-2-results.md 의 offset + `--until-offset` 장애 재현.

---

## 결론

**Migration 측면 합격점**: 
- ✅ 단계적 가역성 (env flag).
- ✅ 실측 기반 판정 (수치 criteria).
- ✅ 도메인 지식 보존 계획 (명시적 채광).
- ✅ 모듈 경계 강화 (유지보수성 3축).
- ⚠️ 72h shadow 최소 기간은 실무 사용빈도에 좌우 — 재판단 필요할 수 있음 (§2 open question).

**리스크 단계화 우수**: 조기 실패 신호 감지 → 범위 축소 재검토 → 재투입 가능한 구조. 전체 실패 시나리오도 단기 (< 2주) 복구 가능.
