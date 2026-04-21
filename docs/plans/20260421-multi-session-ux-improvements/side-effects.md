# side-effects.md

## 리스크 표

| # | 리스크 | 영향 | 대응 |
|---|---|---|---|
| R1 | **Pin API rate limit**: 한 세션에서 긴 답변이 여러 chunk 로 분할될 때 각 chunk 마다 pin 호출 → Telegram API 한도 접근 | pin 실패 (나머지는 정상) | 실패 시 anomaly 로그만 남기고 계속 진행. 대량 동시 pin 우려가 실제로 발생하면 chunk 마다 pin 이 아닌 "마지막 chunk 만 pin" 으로 변경 고려 |
| R2 | **Pin carousel 과잉**: 세션별로 답변이 많이 쌓이면 pin carousel 이 시각적으로 복잡 | UX 저하 | 실사용 데이터 보고 "세션당 최근 N개만 pin" 정책 도입 여부 판단 |
| R3 | **Read-on-leave timing**: 사용자가 s1→s2→s1 왕복 시 s1 pin 이 s2 잠깐 들른 순간 클리어됨 | "실제로 안 읽었는데 읽음 처리" | 왕복 사용 시 의도와 맞음 (들어갔다 나왔으니 봤다 간주). 문제되면 s2 에서 일정 시간 머문 뒤에만 clear 하는 debounce 추가 가능 |
| R4 | **Session kill 중 pin cleanup 실패**: unpinMessage 가 네트워크 오류로 실패 | Telegram 에 orphan pin 남음 | anomaly 로그 + 다음 세션 전환 시 재시도 기회 없음. 사용자 수동 unpin 필요 (단, 매우 드문 케이스) |
| R5 | **/new label/cwd pending 누수**: cancel 없이 사용자가 picker 메시지 무시하고 새 /new 실행 시 덮어쓰기 됨 | 이전 pending 삭제됨 (새 것만 남음) | 올바른 동작 — chatId 단위 1개만 유지 |
| R6 | **자동 전환 race**: hello 수신 직후 registry.setActive 즉시 호출인데, 그 사이에 사용자가 /sessions 로 다른 세션 고를 수 있음 | 사용자 선택이 자동 전환에 덮어씀 가능 | 실제로는 사용자가 "준비됨" 메시지 보기 전에 /sessions 하긴 어려움. 발생해도 사용자 의도 반영이 우선 (덮어쓰기 OK) |
| R7 | **pinMessage(silent=true) 미지원 클라이언트**: 오래된 Telegram 클라이언트는 silent flag 무시하고 알림 뜸 | 과도한 푸시 알림 | 현대 Telegram 클라이언트는 모두 지원. 문제 시 silent flag 대신 개별 설정 |

## 수동 시나리오 매트릭스 (시연 완료 시 체크)

- [ ] /new label cwd 가 실제 label/cwd 로 스폰
- [ ] /new 후 자동 전환 ("자동 전환됨" 메시지)
- [ ] 비활성 세션 reply → pin carousel 에 추가
- [ ] Pin 탭 → 해당 메시지로 점프
- [ ] 세션 떠나면 해당 세션 pin 들 자동 unpin
- [ ] Session kill → pin cleanup

## 기각된 대안

### 대안 A — "세션별 요약 pin" (본 메시지 pin 대신)

각 비활성 세션당 1개의 요약 pin ("📬 s1: 3 replies — tap /sessions") 만 유지.

**기각 이유**:
- Pin 탭해도 실제 메시지로 점프 못함 (요약 메시지일 뿐)
- 사용자가 결국 스크롤 해야 실제 내용 읽을 수 있음
- 본 메시지 pin 하는 것의 UX 이득을 못 살림

### 대안 B — "클라이언트-side unread 뱃지" (Telegram pin 대신)

Telegram 의 기본 unread count 에 의존 (bot 메시지에도 자동으로 붙는 빨간 숫자).

**기각 이유**:
- bot 메시지의 unread 는 채팅 레벨 전체로 관리됨 — 세션별 구분 불가
- 한 chat 여러 세션이 공유하는 구조와 맞지 않음

### 대안 C — "read 감지를 Telegram read receipt 로"

Telegram API 의 read receipt 이벤트로 실제 읽음 여부 감지.

**기각 이유**:
- Bot API 는 사용자의 read receipt 를 제공하지 않음 (프라이버시)
- 로컬에서 "switch in/out" 만 관찰 가능

### 대안 D — "자동 전환 끄기 (사용자 선택)"

`/new` 후 자동 전환을 기본 OFF, config 나 커맨드 플래그로 on/off.

**기각 이유**:
- 현재 사용자 불만이 "자동 전환 안 돼서 혼란" 이라, 기본 ON 이 맞음
- 원치 않으면 `/new` 후 즉시 `/sessions` 로 원래 세션 선택 가능 — 복잡도 추가 없음

## 오픈 이슈

- [ ] **Pin carousel 정리 정책**: 세션당 pin 10개 넘으면 오래된 것 먼저 제거? 현재는 무제한
- [ ] **Read-on-leave debounce**: 들어갔다 바로 나갔을 때의 false-read 허용 정책
- [ ] **`edit_message` 로 같은 메시지 수정 시 pin 상태**: 수정된 메시지의 pin 은 유지됨 (Telegram 기본). 별도 처리 불필요

## Prod readiness 체크리스트

- [x] `bun test` 188 통과
- [x] `bun run typecheck` 0 에러
- [ ] `bun tests/smoke-spawn.ts` (수동)
- [ ] `bun tests/smoke-reply.ts` (수동 — message_ids 포함 확인)
- [ ] 수동 시나리오 1~6 시연
