# side-effects.md

> 설계 2 (Tag-only) 기준.

## 리뷰 결과

2026-04-21 Skeptical + Architect 이중 리뷰 수행. 초기 설계 (dispatcher-routed buffer) 에서 다음 문제가 지적됨:

- 🔴 MCP 시맨틱 미묘: tool call 이 "성공" 반환 후 실제 전송이 async
- 🔴 Core 가 Telegram 에 의존 (`executeReply` 안에 `tg.sendMessage`) — channel-agnostic 원칙 위반
- 🟡 `reply` 만 IPC 경유, react/edit 은 직접 → 비대칭
- 🟡 동시성 race 다수 (`pendingBySession` set/get, flush vs kill, pin vs edit, timer leak)
- 🟡 smoke-reply.ts 가 버퍼링 시나리오 미포함
- 🟡 file size 재검증 누락

→ **설계 2 (Tag-only) 로 선회**. 대부분의 리스크가 설계 수준에서 제거됨.

## 리스크 표 (설계 2 기준)

| # | 리스크 | 영향 | 대응 |
|---|---|---|---|
| R1 | **session_state push 지연 race** — dispatcher 가 push 보내기 전에 Claude 가 reply 호출 | 해당 reply 에 prefix 누락 가능 | Hello 직후 즉시 push, switch 시 즉시 push. race window 는 ms 단위로 좁음. 완벽 보장 원하면 reply 시 IpcBridge 가 dispatcher 에 synchronous 쿼리하도록 변경 가능 (latency 비용) |
| R2 | **IpcBridge 캐시 stale** — MCP 서버가 dispatcher 의 active 상태 변화를 push 받기 전에 reply | 같은 문제 (R1) | 동일 |
| R3 | **dispatcher 재기동 시 카운터 유실** | Pin 카운터가 0 으로 초기화됨 | 메모리라 자연스러운 동작. session_state 는 hello 때 다시 push 되므로 prefix 주입은 정상 복구 |
| R4 | **Pin 편집 race** — 카운터 변화 시 `editMessageText` 다수 동시 호출 | Telegram rate limit 또는 순서 꼬임 | `doUpdateActivePin` 을 serialize 하는 큐 (fixes.md §7) |
| R5 | **text 없는 reply (files only)** 에 prefix 주입 불가 | 파일만 보낸 경우 세션 식별 불가 | caption 필드에 prefix 추가 검토 (후속 PR). 1차 scope 에는 미포함 |
| R6 | **stand-alone 모드 (ipcMode=false)** 에서 prefix 스킵 | 단일 세션용 모드라 prefix 무의미 — 문제 아님 | ToolHandler 에서 ipcBridge undefined 체크로 자연 처리 |
| R7 | **label 변경 시 prefix 불일치** — 사용자가 세션 라벨 수정 가능? (현재 UX 에선 스폰 시 고정) | 작음. 라벨 수정 UX 없음 | 라벨 변경 API 생기면 session_state 재 push 추가 |
| R8 | **pendingCount 메모리 누수** | kill 시 delete 안 하면 누적 | fixes.md §5 에서 kill 경로에 명시적 delete |
| R9 | **react / edit_message 에 prefix 안 붙음** | 세션 혼동 가능? | react 는 사용자 메시지에 붙는 reaction — chat_id + message_id 로 타겟 명확. edit_message 는 기존 bot 메시지 수정 — 이미 다른 prefix 있으면 덮어씀. 둘 다 세션 식별 필요성 낮음 |

## 수동 시나리오 매트릭스

구현 완료 후 수동 검증:

1. **Baseline**: s1 생성 → 메시지 전송 → s1 Claude reply → Telegram 에 prefix **없이** 도착 (active 세션이므로) ✓
2. **Inactive prefix**: s1, s2 생성, s2 active 상태 → s1 에 메시지 전송 (어떻게? inbound 는 active 로만 감 — 이 시나리오는 s1 을 잠깐 active 로 만든 뒤 메시지 보내고 즉시 s2 로 전환해야 재현 가능) → s1 Claude 응답 → Telegram 에 `[backend] ...` 도착 ✓
3. **Pin 카운터**: 위 상태에서 pin 에 `📬 s1: 1` 표시 ✓
4. **전환 시 카운터 리셋**: /sessions → s1 탭 → pin 에서 s1 사라짐, s2 가 inactive 로 바뀌어 session_state push 됨 ✓
5. **다중 응답 누적**: s1 에 메시지 3개 전송 → s2 전환 → s1 Claude 가 3번 reply → pin `📬 s1: 3` ✓
6. **kill 시 cleanup**: s1 카운터 있는 상태에서 /kill s1 → pin 에서 s1 라인 사라짐 ✓
7. **재기동 시 복구**: s1 카운터 있는 상태에서 dispatcher restart → 재기동 후 카운터 0 (유실), hello 시 session_state push 로 active 상태는 정상 복구 ✓

## 기각된 대안

### 대안 A — dispatcher-routed buffer (이전 설계 1)

**기각 이유**:
- MCP tool call 시맨틱 미묘 (성공 반환 vs 실제 전송 async)
- Core 가 Telegram 클라이언트 직접 호출 → channel-agnostic 원칙 위반
- 동시성 race 다수 (queue push/set, flush vs kill, timer leak)
- 구현 복잡도 설계 2 대비 3배
- **설계 2 로 동일한 UX 효과 (세션 식별) 를 훨씬 적은 비용으로 달성 가능**
- 단, "비활성 동안 메시지 안 보이다가 전환 시 몰아서 보기" UX 는 포기

### 대안 B — chat 분리 (per-session Telegram chat)

**기각 이유**:
- Telegram 그룹 관리 오버헤드 (세션마다 생성/삭제)
- 현재 단일 chat 사용자 UX 큰 변화, 이주 비용
- 한 봇 토큰으로 여러 chat 운영 설정 복잡
- **설계 2 대비 구조적 이점은 크지만, 1차 PR 범위로는 과함**
- → 장기 로드맵 (5+ 채널 확장 시) 에 재검토

### 대안 C — synchronous is_active 쿼리

매 reply 마다 MCP 서버가 dispatcher 에 `is_active?` IPC 쿼리 → 응답 받고 전송.

**기각 이유**:
- race 완벽 제거 가능 (설계 2 의 R1/R2 해결)
- 하지만 **모든 reply 에 IPC round-trip 추가** → 수십 ms 지연 누적
- 세션 전환은 드물고, push 지연 race 는 ms 단위로 희귀 → 비용 대비 효과 낮음
- → 필요하면 후속에 옵션으로 추가 가능

### 대안 D — Claude 프롬프트 유도

채널 프롬프트에 "비활성 상태 감지 시 세션 라벨 prefix 붙이기" 추가.

**기각 이유**:
- LLM 은 자신이 active 인지 **알 수 없음** (Claude 에게 그런 개념 없음)
- 확률적 준수 — 결정적 필요
- → 현재 채널 프롬프트의 edit_message 가이드처럼 "보조" 용도 외엔 주 수단 될 수 없음

## 오픈 이슈

- [ ] **R1/R2 race 감수 vs 대안 C 로 강화**: 실사용에서 prefix 누락 사례 발생 시 대안 C 로 전환
- [ ] **text 없는 reply 의 세션 식별**: 파일만 보낸 경우 caption 에 prefix 주입 여부
- [ ] **Pin 정렬**: 세션 id 순 vs count 내림차순 — 일단 id 순 (fixes.md §6 에 정의됨)
- [ ] **stand-alone 모드**: 단일 세션용이라 prefix 불필요. 문서화 필요 (README 또는 채널 프롬프트)

## Prod readiness 체크리스트 (PR 전)

- [ ] `bun test` 171 + 신규 통과
- [ ] `bun tests/smoke-reply.ts` 통과 (reply 경로 회귀)
- [ ] 수동 시나리오 1, 2, 4 통과 (baseline / prefix / 전환 리셋)
- [ ] CLAUDE.md 제1원칙 체크 (Skeptical + Architect 재검토) — 이번 리뷰 반영 후 추가 검토 있으면 기록
