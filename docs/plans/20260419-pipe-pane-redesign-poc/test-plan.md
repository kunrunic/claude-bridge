# test-plan — 스텝별 확인 사항

각 스텝에 **사전 조건 / 실행 / 통과 기준 / 실패 시 행동** 을 명시. Step 3 게이트가 이 문서의 핵심.

## 공통 실행 게이트

모든 스텝 완료 시 아래를 만족해야 다음 스텝으로:

```bash
source venv/bin/activate && python -m pytest tests/ -q
```

- 신규 실패 0건.
- 기존 skipped/xfail 변동 없음.
- 실행 시간이 현재 대비 +50% 이내.

## Step 0 — 기준선 ✅ 완료 (2026-04-19)

### 실행 결과
- 3축 (경계탐색 / scrollback echo / 500줄 창 유실) 각각 대표 케이스 1건씩 선정, `baseline/case-0N-*/` 로 고정.
- 원본 복사: `BUG_REPORT.md` + `tmux_capture.txt`. `logs/` 는 크기 때문에 복사하지 않고 원본 경로 참조.
- 각 케이스 `SUMMARY.md` 에 **현 파서 실패 지점(`file:line`)** + **재설계가 해결해야 할 요구사항** 정리.

### 선정 근거와 scope 분리
- [baseline/README.md](../20260419-pipe-pane-redesign-poc/baseline/README.md) 에 선정/미선정 판단을 문서화.
- 미선정 리포트는 **이미 수정된 계열** (`591f553`, `eecbbce`, `2090abb`) 이거나 분석기와 무관한 tmux target 버그. scope 혼동 방지용으로 링크만.

### 통과 기준 (달성)
- [x] 케이스 3건 이상 문서화 (3건)
- [x] 각 케이스에 현 파서 실패 위치의 `file:line` 링크
- [x] 재현 단서 (원본 `bugreport/<ts>/logs/` 경로 명시)

## Step 1 — pipe-pane 생로그 수집

### 사전 조건
- Step 0 문서화 완료.
- 로컬 tmux 버전 확인 (≥ 3.0 권장, `pipe-pane -o` 지원).

### 실행
1. `BRIDGE_PIPE_PANE=1` 상태로 bridge 기동.
2. 최소 **1시간 실사용** (다양한 flow: 일반 대화, 승인창, ESC 취소, 긴 bypass 작업, compaction 한 번 이상).
3. 수집된 raw 로그를 `tools/fixtures/live-<ts>/` 로 보관.

### 통과 기준 (모두 만족)

| 항목 | 측정 방법 | 기준 |
|------|----------|------|
| append 연속성 | `ls -l raw-*.log` + 내부 timestamp 마커 | 1시간 로그에 10초 이상 공백 없음 |
| capture-pane superset | 같은 시점의 `capture-pane` 결과가 raw 로그 어느 지점의 snapshot 과 일치 | 무작위 5시점 sampling 전부 일치 |
| monitor 무영향 | `core.py` tick 소요 시간 histogram | 기존 대비 p95 증가 ≤ 10ms |
| 회귀 테스트 | `pytest tests/ -q` | green |
| rotation 동작 | 로그 크기 > 20MB 상황 만들기 (또는 MAX_BYTES 임시 1MB 로) | 새 파일로 swap, 기존 파일 read-only 로 flush 됨 |
| off 플래그 | `BRIDGE_PIPE_PANE=0` 재기동 | pipe-pane 프로세스 0, 로그 파일 생성 안 됨, 기존 flow 동일 |

### 실패 시
- monitor 지연 발생 → pipe-pane 명령 자체가 blocking 원인인지 확인. `tmux pipe-pane` 은 fork-exec 형태라 비동기일 것으로 예상되나, 특정 tmux 버전 이슈 가능.
- capture-pane 과 raw 로그 불일치 → ANSI 정규화 차이 (`capture-pane -e` 옵션 유무) 확인. 이 불일치가 재현 가능하면 **Step 2 분석기가 해결해야 할 문제** 로 기록 후 계속.

## Step 2 — 오프라인 분석기 PoC

### 사전 조건
- Step 0 baseline + Step 1 live fixture 확보.
- 최소 fixture 3건 준비.

### 실행
1. `tools/pipe_pane_analyzer.py` 구현.
2. 각 fixture 에 대해 돌려 `events.jsonl` 생성.
3. 수작업 정답지 `expected_events.jsonl` 작성 (한 건 최소 20분 검수 투입).
4. `tests/test_pipe_pane_analyzer.py` 로 regression 고정.

### 통과 기준 (Step 3 의 입력이 됨)

| 항목 | 측정 | 기준 |
|------|------|------|
| 이벤트 시퀀스 일치 | `diff expected_events.jsonl actual.jsonl` | 3건 모두 일치 |
| line-commit 정확도 | 확정된 `⏺ Bash(...)` 줄이 실제 최종 프레임의 해당 줄과 byte-identical | 오탐/미탐 합산 ≤ 2건/fixture |
| ESC 승인 잔해 처리 | Step 0 case 중 ESC-closed 모달 건 | `approval_hide` 이벤트 정확히 발행 + 이후 `⏺` 경계 봉쇄 없음 |
| 스피너 rate | `busy_spinner` diff 이벤트 수 / 초 | 중앙값 ≤ 5/s, p99 ≤ 10/s |
| LoC | analyzer.py 의 tokenizer+screen+commit+dispatch 합계 | 현 `parser.py:214-332` × 0.7 이하 |

### 실패 시
- 이벤트 시퀀스 불일치가 특정 패턴에 집중 → analyzer 보강 or line-commit 규칙 재설계. **1회 사이클 재시도 허용**.
- 두 번 이상 재시도해도 실패 → Step 3 게이트에서 no-go 결정.

## Step 3 — 결정 게이트

### 사전 조건
- Step 2 완료, `tools/eval_pipe_pane.py` 의 자동 체크 결과 존재.

### 실행
1. `python tools/eval_pipe_pane.py` 실행 → `evaluation.md` 생성.
2. README 의 *Decision Criteria* 5항목 pass/fail 확정.
3. 사용자와 함께 결정 기록 → README 의 *Decision Log* 섹션 작성.

### 통과 기준
- Decision Criteria 5/5 전부 pass → **Step 4 진행**.
- 4/5 이상이어도 단일 실패가 rate-limit 같은 운영 이슈면 mitigation 계획과 함께 조건부 진행 가능 (사용자 승인 필수).
- 3/5 이하 → **no-go**. 현 파서 보강 plan (`docs/plans/YYYYMMDD-parser-hardening/`) 로 전환.

### 관측 산출물
- `evaluation.md` — 수치 요약 + 결론.
- README 의 *Decision Log* — 2-3 문장 한국어 요약.

## Step 4~6 — 조건부 (게이트 통과 시)

상세 test-plan 은 해당 단계 진입 시 **별도 plan 폴더** 에서 재작성. 여기서는 가이드라인만:

### Step 4 — 온라인 통합
- **shadow-run**: 기존 파서와 신규 이벤트 소비자를 **동시 구동**, 양쪽 flush 결과를 비교 로그로 찍기. 1주일 이상 무결점 검증 후 프로덕션 스위치.
- 회귀 테스트 `test_bug_*` 전량이 양쪽 경로 모두에서 동일 결과를 내야 함.

### Step 5 — 구 파서 제거
- PR 은 "shadow-run 성공 N일" 증거와 함께.
- `parser.py` 의 pure 함수 중 외부 import 가 남은 경우 migration path 명시.

### Step 6 — 문서 재작성
- AS-IS 섹션을 `docs/design/archive/AS-IS-02-streaming.md` 로 이동.
- AS-IS → TO-BE 매핑표에서 *제거된 개념* (`_response_region`, `StreamQueue.idx/last_fp`, `detect_slip` 등) 을 명시.

## 관측/롤백

- 모든 스텝에서 **기능 플래그** (`BRIDGE_PIPE_PANE`, 추후 `BRIDGE_EVENT_DISPATCH`) 로 즉시 off 가능.
- Step 4 shadow-run 중 양쪽 diff 가 관측되면 dump event `pipe_pane_divergence` 로 기록, 건당 원본 raw 로그 경로 동봉.
- Step 5 이후 롤백은 git revert 수준의 큰 작업이므로, 해당 단계 진입 전 **태그** `pre-pipe-pane-cutover` 를 찍어둔다.
