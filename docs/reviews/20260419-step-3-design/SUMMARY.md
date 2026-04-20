# Step 3 설계 리뷰 — 취합 요약

- 작성일: 2026-04-19
- 대상: [step-3-design.md](../../plans/20260419-pipe-pane-redesign-poc/step-3-design.md)
- 리뷰어: Architecture / Dev·Testability / Reliability·Ops / UX·Telegram / Migration (5명 병렬)
- 개별 파일: 각 role 의 `_positive.md` + `_critic.md` (10 개)

## 판정 요약

5명 모두 **"Gate 조건부 통과, paradigm 전환 지지"** 로 수렴. 단, Step 4-α (shadow
run) 착수 전에 해결해야 할 **공통 blocker** 가 존재 — 아래 §1 참조.

## 1. 복수 critic 이 지적한 공통 blocker

동일 이슈가 2명 이상에서 제기됐다는 것은 설계 공백이 분명하다는 신호.

| # | 이슈 | 제기자 | 요약 |
|---|-----|--------|------|
| C1 | **BridgeAdapter 명세 부재** | Architecture, UX, Migration | 이벤트 → Telegram 매핑, 상태 관리, public interface 가 §3 에 한 줄. 가장 반복적 지적 |
| C2 | **Shadow run diff 도구 미구현** | Dev, Reliability, Migration | "±5% 이내" 판정할 `tools/shadow_diff.py` 가 존재하지 않음. 없이 시작하면 debug 세션이 됨 |
| C3 | **case-02 user-echo 구분 미정의** | Dev, UX, Migration | `send_input` 상관 구현 경로/스키마 없음. Step 4-α 에서 회귀 재발 가능 |
| C4 | **VT ≠ tmux row drift 실측 수단 부재** | Architecture, Dev, Migration | 설계의 "±12 확대" 는 band-aid, 어긋남 측정 도구 없음. case-03 이 최악 시나리오 |
| C5 | **events.jsonl 오염 + message_id 오판** | Migration, UX | cutover/rollback 시 shared state 처리 미명시. 롤백이 실제로 동작하지 않을 수 있음 |
| C6 | **busy_* / session_* 의 Telegram 노출 정책** | UX, Architecture (dispatch 경계) | 메시지 폭탄/배너 노출 규칙 없음 |
| C7 | **Analyzer crash 시 fallback** | UX, Reliability | Step 4-γ 이후 분석기 exception 시 사용자에게 어떻게 보이나. silence? 자동 parser 복귀? |
| C8 | **Case-01/02/03 raw 수집 deadline·담당 불명** | Dev, Migration | Open question (§10 Q1) 에 걸쳐있지만, 실제 Step 4-β 로 언제/누가 옮기는지 없음 |

## 2. 단독 제기 — 설계 반영 필요

각 critic 이 고유하게 잡아낸 가치 있는 지적:

### Architecture 단독
- **A1 — LineCommit.screen 추상화 누수 (HIGH)**: `LineCommit(screen=tuple[str, ...])` 가
  VTScreen 전체 그리드를 하위 모듈에 노출. 매 commit 당 4,800 cell 복사, 책임 분리
  위반. **권고**: RegionTagger 가 자체 VT state 유지.

### Dev/Testability 단독
- **D1 — Sequential offset assertion (30분)**: EventClassifier.feed 입구에
  `assert tl.commit.offset > self._last_offset`. 상위 모듈의 정렬 버그를 조기
  감지.
- **D2 — Critical path 견적**: C2+C3+C4 해결 7.5–11.5 일. Step 4-α 착수 전 이 기간
  확보 필요.

### Reliability/Ops 단독
- **R1 — 30일 내 예측 failure**: disk full → tokenizer incomplete escape →
  VTScreen desync → 3–5 분 응답 누락 → 사용자가 "뭐해?" 입력으로 복구.
  **증상이 FLUSH-EMPTY 로그에 안 남음** — 탐지 불가.
- **R2 — Offset 무결성 safeguard**: inode/size/mtime 을 offset 과 함께 기록, disk
  사전 체크 (`BRIDGE_PIPE_PANE_MIN_FREE_MB`), state 파일 atomic write (2-version).

### UX/Telegram 단독
- **U1 — approval_cancel(method="flush") 정책 공백**: flush 는 "사용자가 승인 안 누른
  채 스트림 종료". 기존은 이런 이벤트 없음. 새로 생긴 이벤트가 사용자에게 보이나,
  언제, 어떤 wording 으로?
- **U2 — busy_enter 폭탄 방지**: 분석기 자체 rate 는 0.07건/초지만, Telegram 으로
  그대로 보내면 세션당 30건 메시지. **suppress 또는 5초 throttle 기본값 권고**.

### Migration 단독
- **M1 — 폐기 정당성 스트레스 테스트 (3개 항목)**:
  - `_response_region` 경계 C (Welcome 배너 multi-boot 구분): 분석기는 offset 만
    보므로 시간 축 정보 손실 가능. **case-05 (multi-boot) synthetic fixture 추가 필요.**
  - `StreamQueue.idx/last_fp` eviction 감지: tuple dedup 은 dedup 은 되지만
    "몇 개 손실" 정보가 사라짐. rotate 를 shadow diff 경계에서 mark 하도록 개선.
  - `is_approval` 3단 체크의 "Yes 아래 divider 부재": Telegram scrollback echo 필터
    역할. envelope 검사만으로 충분한가는 case-02 구현 전까지 불확실.
- **M2 — Frozen parser 강제 메커니즘**: 권장만 있고 강제 없음. `git assume-unchanged`
  또는 pre-commit hook 또는 CODEOWNERS freeze.
- **M3 — "무회귀 2주" 정량 기준**: 현재는 subjective. 제안 — 2주간 `slip`/`suppress`
  이벤트 0건, `block_commit` 대비 `bridge.dispatch` 비율 ≥ 95%.

## 3. 단일 의견으로 수렴된 판정

- **Gate 통과 조건부**: 5명 모두 지지.
- **Paradigm 전환 (parser 폐기)**: 5명 모두 합리적이라고 평가. 단, Migration 은
  "강제 메커니즘 부재" 우려.
- **최대 위험 단계**: Step 4-γ (cutover). 불일치 없음.
- **Step 4-α 시작 전 최소 작업량**: 7.5–11.5 영업일 (Dev critic 견적).

## 4. 설계 수정 제안 (step-3-design.md 에 반영)

우선순위 정렬:

### P0 (Step 4-α 착수 전 필수)
1. **§3a (신규) BridgeAdapter 명세**: 이벤트 × 액션 × Telegram 메시지 매핑 테이블. C1+U1+U2+C6 해소.
2. **§5 Step 4-α prerequisite 추가**: `tools/shadow_diff.py` 구현. C2 해소.
3. **§4 user_prompt 스키마 + case-02 fixture**: send_input 상관 상세 spec. C3 해소.
4. **§6 offset 무결성 safeguard** 보강: inode/size/mtime 기록, disk precheck, state atomic write. R1+R2 해소.
5. **§5 events.jsonl storage 정책**: shadow run 별도 파일 + rollback cleanup 절차. C5 해소.

### P1 (Step 4-α 진행 중 해결)
6. **§3 LineCommit.screen 리팩터링 계획**: 별도 과제로 티켓화 (Step 4-α 실험 후 데이터 기반 결정). A1 해소.
7. **§6 Analyzer crash fallback**: exception → log + Telegram status 메시지 + auto-restart ≤ 3회. C7 해소.
8. **§10 Q1 → Step 4-β deliverable**: case-01/02/03 수집을 기한/담당 명시로 승격. C8 해소.
9. **§5 Step 4-γ frozen parser 강제**: pre-commit hook 또는 CODEOWNERS. M2 해소.
10. **§9 Step 5 exit 정량 기준**: slip/suppress 0건, dispatch 비율 ≥ 95%. M3 해소.

### P2 (Step 4 진행 중 관찰, 필요 시 조치)
11. **case-05 (multi-boot) synthetic fixture**. M1 해소.
12. **row drift 모니터링**: shadow run 중 `match_distance` 기록. C4 후속.
13. **Sequential offset assertion 추가** (30분). D1 해소.

## 5. 설계 관점 재확인

### 강점 (5명 공통)
- 6 단계 파이프라인의 **단일 책임 분리**.
- offset-based **결정적 재현** (`--until-offset N`).
- paradigm 전환의 **일관성** (hybrid 없음).
- 가역적 **3단계 migration** (shadow → parity → cutover).

### 약점 (5명 공통)
- **BridgeAdapter = 블랙박스** — Step 4-γ 이전에 spec 확정 필수.
- **Shadow run ≠ proof** — 72h 로 포착 안 되는 증상 다수. diff 도구 + 능동 모니터링 필요.
- **"권장" 의 비중이 큼** — 강제 메커니즘 (pre-commit, atomic write, disk precheck) 필요.

## 6. 최종 권고

- Step 4-α 착수 **전** P0 5개 항목 완료.
- P1 5개 항목은 Step 4-α 기간에 병행 해결, Step 4-β 진입 조건에 포함.
- Step 4-γ cutover 전에 5명 critic 재검토 요청 (특히 BridgeAdapter spec).

본 SUMMARY 의 P0/P1 반영 내역은 step-3-design.md §13 "Critic 반영" 절에 기록.

## 7. 2차 사용자 피드백 (2026-04-19 PM)

SUMMARY 를 읽은 사용자가 4개 재해석을 제기 → step-3-design.md §13.11 에 반영 (§13.11
이 이전 §13.2/§13.3/§13.5/§13.7 보다 우선).

| # | 재해석 | 결론 |
|---|--------|------|
| C2 | shadow_diff "±5%" 의미 약함 | **집합 포함 기반** 으로 재정의 (§13.11.a) |
| C4 | row drift 측정 필요한가 | **불요** — 분석은 offset stream 순서 기반 (§13.11.b) |
| C3 | byte-offset 여전히 유효한가 | **아니오** — region-based echo 억제 (§13.11.c) |
| HIGH multi-boot | welcome 구분 vs compact 이벤트 | **compact 1급 이벤트** 로 승격 (§13.11.d) |

Net 효과: drift 작업 삭감 (~1–2일) ↔ compact·echo 구현 추가 (~1–2일), Step 4-α
착수 기간은 유지하면서 설계 정합성 향상.
