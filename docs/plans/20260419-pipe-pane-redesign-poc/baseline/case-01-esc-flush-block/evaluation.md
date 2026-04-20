# case-01 — Decision Gate 평가 (raw+VT 전환 실효성)

SUMMARY.md 의 "재설계가 해결해야 할 요구사항" 을 Step 3 게이트 언어로 재평가.

## 현재 아키텍처의 실패 메커니즘 (압축)

`_response_region` (parser.py:228-238) 가 **tail 제한 없이** 전체 pane 을 역방향으로
`"Do you want to proceed"` 스캔 → scrollback 에 남은 ESC 취소 모달 텍스트가 매칭
→ 모달 앞 divider 로 `end` 고정 → 이후 6시간 동안 모든 `⏺` 블록이 응답 영역 밖.
`_flush_completed` 는 `new=0` 만 반복, 전송 0 건. scrollback 이 500줄 창 밖으로
밀려날 때까지 자기 해소 불가.

## raw+VT 전환 시

### Category-A (raw 가 **유일하게** 해결하는가): **강**

현재 구조는 "매 tick 전체 pane 을 다시 해석" 하는 snapshot 모델이라서 6시간 전의
scrollback 텍스트가 계속 현재 판정에 영향을 준다. raw+VT 는 **append-only 이벤트
스트림** 이므로:

- `approval_show` / `approval_hide` 를 **byte offset 로 고정된 과거 이벤트** 로 기록.
- ESC 취소는 `approval_hide` 이벤트로 soldify — 이후 분석은 이 offset 앞쪽을
  재해석하지 않음.
- 새 `⏺` 블록은 `approval_hide` 이후의 stream event 이므로 과거 모달 텍스트와
  원천적으로 섞일 수 없음.

이 "과거 scrollback 이 현재 판정을 오염" 구조는 snapshot 패러다임에 내재. regex 를
아무리 좁혀도 TMUX_SCROLL_LINES 창 안에서는 동일 패턴이 살아있으면 재발. **구조적
해결은 raw 만 가능.**

### Category-B (부수적 아키텍처 청소): **중**

- `_response_region` 의 "tail 비대칭" 버그 (is_approval 은 60줄, _response_region 은
  무제한) 같은 **정책 불일치 클래스 자체**가 사라짐 — 이벤트 기반은 tail 개념 없음.
- `awaiting_approval` 상태 누수 (case-01 가설 B 의 보조) 도 `approval_hide` 이벤트
  수신 시점에 자동 초기화 가능.

### Category-C (미래 내성): **중**

Claude Code 가 모달 문구 / 레이아웃을 바꿔도, 모달의 **생애주기 이벤트** (열림/닫힘)
가 구조적 신호 (box-drawing 진입/퇴장, cursor home + clear 시퀀스) 로 잡히면
영향 최소.

## Decision Gate 기여도

**강 (A/B/C 모두 해당)**. snapshot 모델의 내재 한계를 정확히 찌르는 케이스.
`_response_region` regex 를 tail 60 으로 제한하는 건 응급 처치일 뿐, 비슷한 잔존
마커 계열 (`bc6a015`, `4c20696` 회귀 2건) 이 반복될 구조는 그대로 남음.

## Step 2 분석기 요구사항 (이 케이스에서 추출)

1. **이벤트 생애주기**: `approval_show` (모달 진입) ↔ `approval_hide` (ESC 또는 승인/거부)
   를 byte offset 로 쌍 기록.
2. **과거 재해석 금지**: 분석기 커서는 단조 증가. `approval_hide` 이후 커서는 다시
   그 이전으로 돌아가지 않음.
3. **structural detection**: "Do you want to X" 문구 의존 최소화. 가능한 신호:
   - 특정 box-drawing 문자 블록 (`╭─╮ / │ / ╰─╯`) 이 한 번에 등장
   - `\x1b[?1049h` (alt screen enter) 같은 모달 전환 시퀀스가 있으면 그것 우선
   - `❯ 1. Yes` + 직상단 15줄 내 "Do you want to" 의 broad 매치
