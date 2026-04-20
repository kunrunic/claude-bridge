# case-02 — 사용자가 붙여넣은 승인 박스 텍스트가 라이브 승인으로 오탐

- 원본: [`bugreport/20260417_164104/`](../../../../../bugreport/20260417_164104/)
- 수집 파일: `BUG_REPORT.md`, `tmux_capture.txt` (이 폴더)

## 한 줄 현상

사용자가 **이전 세션 Edit 승인창 스크린샷의 텍스트** (`Do you want to make this edit to parser.py?` + `❯ 1. Yes`) 를 텔레그램으로 붙여넣기. 봇이 pane 에 literal 입력 → 그 echo 를 라이브 승인창으로 오인해 `[AI-APPROVAL] Read` 를 반복 발사. 사용자가 Yes 를 눌러도 echo 는 그대로라 루프 지속.

## 현 파서 실패 지점

- `bridge/parser.py:98-120` (`is_approval`) — 3단 검증 (`Yes` 패턴 + 위 15줄 내 `Do you want to…` + Yes 아래 입력 divider 부재). scrollback echo 에서도 이 세 조건이 **모두** 참이 되는 구조 (사용자 입력이 그대로 pane 에 주입된 상태라 divider 가 echo 아래 없을 수 있음).
- `summarize_approval` (`parser.py:337-356`) — APPROVAL_SCAN_LINES=30 범위에서 툴명 매치 → 근처 echo 의 `Read` 토큰을 라벨로 채택.
- 핵심: "이 텍스트가 **사용자 입력의 echo** 인지 **Claude 가 렌더한 UI** 인지" 를 frame snapshot 만 보고는 구분 못함.

## 재설계가 해결해야 할 요구사항

1. 승인 박스는 Claude 가 그린 **UI 오버레이** 일 때만 `approval_show` 이벤트. 사용자 입력이 echo 되는 구간은 `user_prompt` 스트림으로 분리.
2. line-commit 판정 시 "커서 아래 대기 중인 입력 프롬프트" 와 "모달 박스 렌더링" 을 ANSI 이벤트 (box-drawing 문자 위치, 커서 고정 패턴) 로 구분.
3. 같은 scrollback 영역이 여러 tick 에서 중복 해석되지 않도록 append-only offset 고정.

## 부가 단서

- BUG_REPORT 타임라인: 14:19:28 `[USER→BOT]` → `[BOT→AI] forwarded` (send-keys -l) → 14:20:19 `[AI→BOT] pre-approval` → 14:20:20 `[AI-APPROVAL] Read`.
- 사용자 Yes(14:25:54) 이후에도 동일 패턴 재발 → frame 기반으로는 자기 해소 불가한 구조적 오탐.
