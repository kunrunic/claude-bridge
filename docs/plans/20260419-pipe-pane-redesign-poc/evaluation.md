# evaluation — Decision Gate 집계

Step 3 (`docs/plans/20260419-pipe-pane-redesign-poc/README.md`) 의 게이트 판단을 위해
baseline 4 케이스를 raw+VT 전환 실효성 관점에서 교차 평가.

작성일: 2026-04-19
근거: `baseline/case-0{1..4}/evaluation.md` 각각의 Category-A/B/C 판정.

## Category 정의

- **A** — raw+VT 로 가야만 구조적으로 해결되는가? (current arch regex/알고리즘 개선
  으로는 근본 해결 불가)
- **B** — 아키텍처 전환으로 **부수적** 버그 클래스 / 복잡도 / 침묵 경로가 사라지나?
- **C** — 미래 내성 — Claude Code 가 문구/레이아웃을 바꿨을 때 견디나?

## 집계표

| 케이스 | 축 | A | B | C | Gate 기여 |
|---|---|---|---|---|---|
| [case-01 ESC flush 봉쇄](baseline/case-01-esc-flush-block/evaluation.md) | 경계 탐색 scrollback 오염 | **강** | 중 | 중 | 강 |
| [case-02 scrollback echo](baseline/case-02-scrollback-echo/evaluation.md) | 사용자 echo vs UI 구분 | 약~중 | 중 | 약 | 중 |
| [case-03 StreamQueue eviction](baseline/case-03-streamqueue-eviction/evaluation.md) | 500줄 창 구조적 한계 | **최강 (keystone)** | **강** | **강** | **최강** |
| [case-04 Edit 승인 wording](baseline/case-04-edit-approval-wording/evaluation.md) | 문구 의존 + queue_slip | 약 | **강** | 중~강 | 중 |

## 판단

**전환 방향성 지지: 예 (strong)**. 단, 근거의 무게중심은 다음과 같음:

1. **case-03** 이 단독 keystone. 이 케이스 하나로 "snapshot 대 stream 의 패러다임
   격차" 는 증명됨 — 500줄 창 구조는 regex 로 고쳐지지 않음. raw 전환이 필요한가에
   대한 답은 YES.

2. **case-01** 이 두 번째 축. 과거 scrollback 텍스트가 현재 판정을 오염시키는
   snapshot 의 내재 한계. 회귀 2회 (`bc6a015`, `4c20696`) + 오늘 case-01 까지 3회
   반복된 **버그 클래스 자체**를 지움.

3. **case-02, case-04** 는 단독으로는 current arch 개선 여지가 있음 (regex 확장,
   send_input 상관 추가). 하지만 raw+VT 로 가면 같은 문제를 훨씬 단순한 원리
   (region attribution, event lifecycle) 로 해결. **케이스별 patch 누적의 관성**을
   끊을 근거.

## 주의사항 / 반론 고려

- **Scope creep 위험**: raw+VT 전환은 LoC 증가. 현재 `parser.py` 내부 개선의 총합
  대비 순 LoC 증가량은 Step 2 구현해봐야 정확히 비교 가능.
  → Step 3 게이트에 **LoC 비교표** 추가 항목 신설 필요.

- **운영 리스크**: pipe-pane 의 disk write 부담, rotation, 분석기 크래시 시 fallback.
  side-effects.md 의 R1-R8 이 이 항목들을 커버하지만 실운영 24시간 이상 데이터 필요.
  → Step 1 의 "1시간 실로그" 기준을 **24시간** 으로 상향 권장.

- **Claude Code UI 안정성 가정**: 구조 기반 검출도 box-drawing 문자 / alt-screen
  / 커서 시퀀스 패턴이 어느 정도 안정적이어야 성립. 현재는 경험적으로 안정 — 추가
  검증 불요.

## Step 2 착수 전에 확정할 것

baseline 4 케이스의 `evaluation.md` 가 뽑아낸 분석기 요구사항 union:

### 이벤트 스키마 (확정 후보)
- `block_commit(offset, type=response|tool_call, text)` — `⏺` 블록 1개
- `approval_show(offset, tool_hint, summary_text)`
- `approval_confirm(offset, choice)`
- `approval_deny(offset, choice)`
- `approval_cancel(offset, method=esc)`
- `busy_enter(offset, label)` / `busy_exit(offset)`
- `session_resume(offset)` / `session_boot(offset)`

### 상태 기계 규칙
- `approval_show` ↔ `approval_*` (confirm/deny/cancel) 쌍.
- `block_commit` 은 `approval_show` 중에는 발화 **보류** (승인박스 해소 후 발화).
- 모든 이벤트는 **byte offset 고정**. 과거 재해석 금지.

### region tagging
- input_box / content / modal_overlay — line 단위 region 속성 기록.
- user echo 와 Claude UI overlay 는 region 으로 구별.

### dedup 원리
- `(offset, event_type)` 튜플의 first emit 만 유효. StreamQueue idx/last_fp 로직
  기계적 제거.

## 결론 — Step 3 게이트 판정 (preliminary)

**게이트 통과 전제하의 Step 2 착수 승인**. 단:
- Step 2 분석기 구현 중 LoC/복잡도 측정 필수.
- Step 1 의 live log 수집 기간 24시간 이상으로 연장 권장.
- 위 이벤트 스키마 / 상태 기계 / region tagging / dedup 원리 4점 을 **Step 2 spec**
  으로 확정 후 코딩 시작.
