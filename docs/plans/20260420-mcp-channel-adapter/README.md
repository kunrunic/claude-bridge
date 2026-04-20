# 2026-04-20 MCP 채널 어댑터 전환

## 한 줄 요약

`⏺` 블록 파싱·포워딩 계층을 폐기하고, Claude Code 의 `claude/channel` MCP 프로토콜
에 맞춘 **자체 구현 채널 어댑터**로 대체한다. 브릿지는 세션 생명주기·라우팅·관찰성
I/O 플러밍만 소유한다.

## 배경 트리거

- **20260420_123305**: transient `⏺ Reading 1 file… (ctrl+o to expand)` 가 앵커로
  고정된 뒤 prompt echo 로 변환되며 같은 턴의 모든 응답 블록이 영구 stranded.
- 위 증상의 fix 로 `_ACTIVE_BLOCK_RE` 에 패턴을 추가하는 시도는 verbose 모드나
  Claude Code UI 버전 변경에서 회귀 가능 — 문자열 기반 분류 자체가 땜질이라는
  결론.
- 과거 누적 이슈들 (20260417_095801 eviction, 20260420_072013 race) 도 공통적으로
  "pane 파싱 + 앵커" 레이어의 취약성에 뿌리를 둠.

## 문서

| 파일 | 내용 |
|---|---|
| [design.md](design.md) | 본 설계 전문 (§1 ~ §9) |
| spike-log.md (예정) | 단계 1 공식 플러그인 spike 결과 |

## 상태

- 2026-04-20: 방향성 확정 (사용자 승인)
- 다음: 단계 1 spike 실행
