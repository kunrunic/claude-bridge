# pipe-pane 기반 스트리밍 재설계 PoC

- 작성일: 2026-04-19
- 작성자: sjun + Claude (설계 의견 교환)
- 상태: **PoC 설계 단계** (코드 변경 전)
- AS-IS 설계: [../../design/02-streaming-pipeline.md](../../design/02-streaming-pipeline.md), [../../design/03-parser-and-boundaries.md](../../design/03-parser-and-boundaries.md)
- 최근 회귀 이력: [../20260418-esc-flush-recovery/README.md](../20260418-esc-flush-recovery/README.md), 커밋 `bc6a015`, `4c20696`

## 배경

현재 bridge 는 매 tick(1s) `capture-pane -S -500` 스냅샷을 새로 받아 `_response_region` 으로 "현재 turn 범위" 를 역방향 탐색한다 (`bridge/parser.py:214-257`). 이 구조의 구조적 취약점:

1. **tail 정책 비대칭** — `is_approval` tail 60 vs `_response_region` 경계 A 전체 역방향 등 함수별 스캔 깊이가 다름 → scrollback 잔해(특히 ESC 로 닫힌 승인 모달)가 경계 탐색만 당겨 flush 봉쇄. 최근 세 번의 회귀 전부 이 계열.
2. **스냅샷 재해석의 본질적 중복** — 이미 내보낸 `⏺` 블록을 매 tick 다시 파싱하고 `StreamQueue.idx/last_fp` 로 dedup. slip 판정 실패 경로는 로그도 침묵 (`03-parser-and-boundaries.md:123`).
3. **관측 단절** — 파서가 실패했을 때 "어떤 pane 문자열을 어떻게 해석했는가" 를 사후 재현하기 어려움 (raw ANSI 를 버린 `strip_ansi` 결과만 dump 됨).

## 제안 방향

`tmux pipe-pane -o` 로 **ANSI 포함 생로그를 append-only 파일** 에 흘리고, 분석기는 파일 offset 을 cursor 로 삼아 앞으로만 읽는다. 이벤트 단위 분류 (`⏺` 완료 / `❯` 대기 / `* <단어>` busy / 승인 박스 show-hide 등) 로 전환하면 경계 탐색 자체가 필요 없다.

**핵심 가설**: 경계 탐색 복잡도가 "ANSI line-commit 판정" 복잡도보다 **작다** 는 것을 실측으로 검증해야 한다. 그래서 본 설계는 먼저 non-invasive 하게 raw 로그를 수집하고, 오프라인 분석기 PoC 를 돌려 결정 게이트(Step 3)를 통과한 경우에만 온라인 통합(Step 4+)으로 진행한다.

## 스텝 개요

| Step | 제목 | 의도 | 가역성 | 완료 조건 |
|------|------|------|--------|-----------|
| 0 | 기준선 캡처 | 현 분석기의 실패 패턴을 고정된 fixture 로 박제 | 문서만 | bugreport 최신 N건의 실패 지점 + raw pane 텍스트 아카이브 |
| 1 | pipe-pane 생로그 수집 | 관찰 데이터 확보, 기존 동작 무변경 | 완전 가역 (옵션 off 로 즉시 복귀) | baseline 4 케이스 중 최소 1건의 실 raw log 확보 + 기존 테스트 green (**2026-04-19 완료**) |
| 2 | 오프라인 분석기 PoC | raw 로그에서 이벤트 스트림 추출 | repo 내 tools/ 스크립트, 프로덕션 무영향 | Step 0 fixture 에서 이벤트 경계 올바르게 판정 |
| 3 | **결정 게이트** | 재설계 착수 여부 판단 | N/A | 아래 *Decision Criteria* 통과 |
| 4 | 온라인 분석기 통합 (조건부) | bridge 내부에 이벤트 디스패처 장착 | shadow-run 기간 중 가역 | capture-pane 결과와 raw 분석기 결과 diff 없음 |
| 5 | 기존 파서 경로 제거 (조건부) | `_response_region` / `StreamQueue` 축소 | 재도입 비용 큼 | 전 회귀 테스트 raw-log fixture 로 치환 |
| 6 | 문서 재작성 (조건부) | design/02, 03 TO-BE 반영 | 문서 | AS-IS → TO-BE 매핑표 |

각 스텝의 구체적 변경 지점은 [fixes.md](fixes.md), 확인/통과 기준은 [test-plan.md](test-plan.md), 리스크/기각 대안은 [side-effects.md](side-effects.md).

## Decision Criteria (Step 3 게이트)

PoC 분석기가 **전부 만족할 때만** Step 4 진행:

1. **재현성**: [baseline](baseline/README.md) 3개 케이스(case-01/02/03) 전부에 대해 분석기가 `⏺` 블록 경계/이벤트 시퀀스를 현 파서보다 **같거나 더 정확히** 판정 (각 `SUMMARY.md` 의 "재설계가 해결해야 할 요구사항" 을 충족).
2. **승인 박스 show/hide**: 로그 스트림에서 모달 등장/ESC 소멸이 오탐 0건으로 판별됨.
3. **line-commit 복잡도**: ANSI tokenizer + commit 판정 코드 **LoC ≤ 현 `parser.py:214-332` 의 70%** (경계 A/B/C + `_is_block_active` + `extract_response_blocks` 합계).
4. **스피너 rate**: `* <단어>` diff 이벤트가 초당 ≤ 5회 (Telegram edit rate-limit 대비 안전 마진). 초과 시 coalescing 전략이 간결한지 확인.
5. **관측성**: 실패 케이스에서 "어느 token 에서 어떤 이벤트로 분류됐는지" 가 dump 이벤트로 재구성 가능.

3개 이하 통과 시 → Step 4 중단, 현 파서 보강 방향으로 회귀. 본 plan 폴더는 **삭제하지 않음** (plans 규칙).

## 변경 파일 범위 (예상)

Step 1 (확정):
- `bridge/tmux.py` — pipe-pane start/stop 헬퍼 추가
- `bridge/core.py` — session attach 시 pipe-pane 기동 / 종료 훅
- `bridge/config.py` — 로그 경로/rotation/on-off 플래그
- `bridge/dump.py` — raw 로그 파일 경로를 dump 이벤트에 동반 기록

Step 2 (확정):
- `tools/pipe_pane_analyzer.py` — 오프라인 CLI (신규)
- `tools/fixtures/` — Step 0 에서 확보한 실제 raw 로그 (테스트 fixture)

Step 4~6 (조건부): Step 3 게이트 통과 후 확장 plan 폴더(`plans/YYYYMMDD-pipe-pane-online/`) 를 별도로 쓴다. 본 plan 은 PoC 완료까지만 담당.

## 체크리스트

### Step 0 — 기준선 ✅ 완료

- [x] 회귀 3건 선정 (3축: 경계탐색 / scrollback echo / 창 유실)
- [x] `baseline/case-01-esc-flush-block/` — `bugreport/20260418_094744/` (ESC flush 봉쇄)
- [x] `baseline/case-02-scrollback-echo/` — `bugreport/20260417_164104/` (echo 승인 오탐)
- [x] `baseline/case-03-streamqueue-eviction/` — `bugreport/20260417_095801/` (500줄 창 유실)
- [x] 각 케이스 `SUMMARY.md` 에 현상 + 실패 code path + 재설계 요구사항 정리
- [x] 인덱스: [baseline/README.md](baseline/README.md) — 참고 사례(`bugreport/20260417_172211`, claude-bridge 레포 2건) 도 함께 링크

### Step 1 — 생로그 수집 ✅ 완료 (2026-04-19)
- [x] `bridge/tmux.py` pipe-pane 헬퍼 추가 (`start_pipe_pane` / `stop_pipe_pane`)
- [x] `bridge/core.py` attach/detach/rotate 훅 연결 (`_pipe_attach` / `_pipe_detach` / `_pipe_maybe_rotate`)
- [x] 로그 경로: `~/.claude-bridge/panes/<session>/raw-<startTs>.log`
- [x] rotation: 20MB 초과 시 새 파일 (30초 간격 크기 체크)
- [x] on/off 플래그 `BRIDGE_PIPE_PANE=1` (기본 on)
- [x] 단위 테스트: `tests/test_pipe_pane_tmux.py` 8개 (모두 green)
- [x] 실사용 수집: **case-04-edit-approval-wording** (452KB raw + 15KB events) 확보
  — 원래 "1시간 연속" 기준은 현재 프로덕션 상태로 불가능. Step 2 분석기 fixture 용
  **패턴 다양성** 기준으로 전환 (아래 Step 2 완료 조건 참조).

### Step 2 — 오프라인 분석기
상세 스펙: [step-2-spec.md](step-2-spec.md)
- [ ] ANSI tokenizer (CSI/OSC/SGR 구분, CR/LF/cursor-move 인식)
- [ ] VT 가상 스크린 (24×200 기본, 필요 시 resize 이벤트 추적)
- [ ] line-commit 판정 규칙 확정 (후보: "cursor 가 해당 줄 아래로 이동 후 N tick 동안 되돌아오지 않음")
- [ ] region tagging (input_box / content / modal_overlay)
- [ ] 이벤트 enum (evaluation.md 확정): `block_commit`, `approval_show/confirm/deny/cancel`, `busy_enter/exit`, `session_boot/resume`, `user_prompt`, `compaction_start`, `limit`, `trust_prompt`, `resume_picker`
- [ ] 상태 기계: `approval_show` 중 block 발화 보류, offset dedup
- [ ] baseline 4 케이스 fixture 회귀 테스트 (`tests/test_pipe_pane_analyzer.py`)
- [ ] 출력 포맷: JSON Lines — bridge 내부 통합 시 그대로 이벤트 소스로 쓸 수 있게
- [ ] **Step 1 → Step 2 교체 pass criterion**: 각 baseline case 의 `evaluation.md` 의
  "Step 2 분석기 요구사항" 섹션 항목을 fixture 대상으로 검증

### Step 3 — 게이트
- [ ] 위 Decision Criteria 5개 항목별 pass/fail + 근거 기록
- [ ] 결과를 본 README 하단 `Decision Log` 섹션에 추가
- [ ] 통과 → 새 plan 폴더 생성, 미통과 → 회귀 방향 plan 생성

## Decision Log

(Step 3 완료 시점에 기록. 현재 공란.)

## 참고

- 상위 plans 인덱스: [../README.md](../README.md)
- AS-IS 전체: [../../design/README.md](../../design/README.md)
- 유사 체계로 가기 전 검토된 대안: [side-effects.md](side-effects.md) 의 *기각된 대안* 섹션
