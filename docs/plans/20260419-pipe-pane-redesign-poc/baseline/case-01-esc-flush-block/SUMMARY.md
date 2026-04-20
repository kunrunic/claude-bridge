# case-01 — ESC 이후 flush 봉쇄

- 원본: [`bugreport/20260418_094744/`](../../../../../bugreport/20260418_094744/)
- 수집 파일: `BUG_REPORT.md`, `tmux_capture.txt` (이 폴더)
- 기타 증거 (필요 시 원본 경로 참조): `logs/2026-04-18.log` (~3.5KB)

## 한 줄 현상

승인 모달 `/esc` 취소 이후 **모든 `⏺` 응답이 텔레그램 flush 0건** 으로 봉쇄. monitor 는 `[AI-BUSY]` edge 를 계속 감지해서 Claude 가 turn 을 끝낸 것은 확실.

## 현 파서 실패 지점

- `bridge/parser.py:228-238` (`_response_region` 경계 A) — **tail 제한 없이** 전체 pane 역방향으로 `"Do you want to proceed"` 를 찾아 그 위 divider 로 `end` 를 당김.
- scrollback 에 남은 ESC 모달 텍스트(42~56 행) 가 매칭되어 응답 영역이 모달 앞에서 끊김 → 147+ 행의 실제 `⏺` 블록이 영역 밖으로 제외됨.
- 비대칭: `is_approval` 은 같은 키워드를 **tail 60줄로 제한** (`parser.py:106`) 해서 False → `awaiting_approval=False` 경로로 들어가는데, `_response_region` 만 탐지되어 flush 0.

## 재설계가 해결해야 할 요구사항

1. "현재 turn 영역" 개념 없이 **append-only 스트림의 새 이벤트만** 내보낸다 (과거 frame 재스캔 금지).
2. ESC 로 닫힌 모달은 `approval_show` → `approval_hide` 이벤트 쌍으로 분명히 구분되어야 한다.
3. `approval_hide` 이후에 발생한 `⏺` 블록은 **추가 상태 의존 없이** 바로 block_start 이벤트로 분류.

## 현 `_flush_completed` 결과 (참고)

`logs/2026-04-18.log` 기준 02:21:20 이후 `flush_peek new=0` 이 계속 기록됨. `take_new` 는 빈 리스트. 로그로는 "봉쇄" 가 관측되지만 원인 단서는 `flush_peek` 의 `response_region_end` 가 비정상적으로 작은 값일 것 (Step 2 분석기로 재현 대상).
