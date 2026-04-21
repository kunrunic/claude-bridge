# side-effects.md

## 리스크 표

| # | 리스크 | 영향 | 대응 |
|---|---|---|---|
| R1 | **dispatcher 재기동 시 버퍼 유실** | 비활성 세션에서 작성된 답변이 사라짐. 사용자는 Claude 에게 재요청해야 복구 | README / 채널 프롬프트 에 "재기동 시 비활성 세션 pending 답변 유실 가능" 명시. SQLite 는 기각 (아래 "기각된 대안" 참조) |
| R2 | **IPC round-trip latency** | 기존 직접 전송 대비 Claude 의 reply 툴 응답 시간 증가 (수십 ms). 사용자 체감 영향은 미미하지만 테스트 타이밍에 영향 | 테스트에 `await` + `sleep` 충분히. Prod 영향은 거의 없음 |
| R3 | **버퍼 메모리 누수** | 비활성 세션에 대량 reply 누적 시 메모리 증가. 특히 dead 세션을 정리 안 하면 leak | kill 시 버퍼 명시적 delete. 선택적으로 버퍼 최대 크기 제한 (예: 100건) 추가 고려 |
| R4 | **reply_request timeout (30s) 중 dispatcher 미응답** | Claude 의 reply 툴 호출이 실패로 돌아감. Claude 는 "답변 실패" 로 인식 | 30s 는 충분히 긴 값. dispatcher 가 그만큼 막히면 이미 다른 문제. Timeout 발생 시 anomaly 로그 |
| R5 | **label prefix 가 Claude 응답 자체에 포함** | 이미 Claude 가 응답에 라벨 언급하는 경우 `[s1] [s1 says...] ...` 중복 | prefix 는 "dispatcher 가 비활성 flush 시에만" 붙임. 활성 reply 는 prefix 없음 — 중복 가능성 낮음 |
| R6 | **Pin 편집 rate limit** | 버퍼 count 바뀔 때마다 pin 편집 → Telegram API 제한 가능 | count 변화 시만 편집 (이미 설계 반영). 변화 없으면 skip. 필요 시 debounce 추가 |
| R7 | **동시성**: 같은 세션에 reply_request 가 병렬로 여러 개 들어옴 | 순서 뒤집힘 / race | Claude 는 본질적으로 순차 툴 호출 (1개 세션당 1개 in-flight). 다중 세션 간 race 는 request_id 로 correlation 유지하므로 안전 |
| R8 | **기존 `reply_sent` 시그널과 중복** | 현재 `reply_sent` 는 애니메이션 종료용. 새 `reply_response` 도 "완료" 의미. 이중 시그널 | `reply_sent` 를 deprecate 하고 `reply_response` 로 통합 OR `reply_sent` 는 유지하되 의미 재정의 (애니메이션 전용). **결정 필요**. 1차는 병존, 후속 정리. |

## 수동 시나리오 매트릭스

구현 완료 후 반드시 수동 검증해야 할 케이스:

1. **Baseline**: s1 생성 → 메시지 전송 → s1 Claude reply → Telegram 에 prefix 없이 도착 ✓
2. **간단 버퍼**: s1, s2 생성 → s2 활성 → s1 에 메시지 전송 (이때 inbound 는 s2로 감, 별개) → 시나리오 변경: s1 을 활성으로 만들어 메시지 전송 → 즉시 s2 로 전환 → s1 Claude 응답 완료 → Telegram 에 도착 안 함 (버퍼링) → pin 에 `📬 s1: 1` ✓
3. **전환 flush**: 위 상태에서 /sessions → s1 탭 → 전환 즉시 s1 답변 Telegram 에 `[backend] ...` prefix 로 도착 ✓
4. **다중 버퍼**: s1 에 3개 메시지 빠르게 전송 → s2 로 전환 → s1 Claude 가 3개 다 응답 → 3개 모두 버퍼링 → pin `📬 s1: 3` → s1 전환 → 3개 순서대로 flush ✓
5. **kill 시 discard**: s1 버퍼 있는 상태에서 /kill s1 → 버퍼 사라짐, Telegram 도착 안 함, pin 에서 s1 라인 사라짐 ✓
6. **Dispatcher 재기동**: s1 버퍼 있는 상태에서 dispatcher restart → 재기동 후 pin 에 pending 없음, 버퍼 유실됨 (예상 동작) ⚠️
7. **Timeout**: dispatcher 를 blocking 시키거나 강제 지연 → 30s 후 Claude 의 reply 툴이 실패 반환 ✓

수동 확인 전에 smoke-reply.ts 통과 확인.

## 기각된 대안

### 대안 1: SQLite 로 영속 버퍼

**기각 이유**:
- claude-bridge 는 per-user local 툴, 재기동 빈도 낮음
- 이미 `reconcileOrphans` 가 재기동 시 세션 자체 리셋하는 철학. 버퍼만 살아남으면 일관성 깨짐 (orphan 버퍼가 존재 안 하는 세션을 가리킬 수 있음)
- 의존성 / schema migration / WAL handling 등 복잡도 추가
- 유실되어도 사용자가 "다시 답해줘" 요청으로 복구 가능
- → **재기동 안전성 필요하면 후속 PR 로 검토**. 1차는 memory 충분

### 대안 2: Pin 본문에 preview 저장 (dispatcher 메모리 대체)

**기각 이유**:
- Telegram pin 메시지 4096자 한계. 여러 세션 × 여러 메시지 누적 시 초과
- 편집 race / rate limit 관리 복잡
- 버퍼 "기억" 과 "표시" 를 섞은 것. 별도 책임을 하나의 Telegram 상태에 위임하는 나쁜 패턴
- → Pin 은 "count 인디케이터" 역할로 제한. 본문 보관은 메모리

### 대안 3: 채널 프롬프트로 Claude 가 스스로 `[s1]` prefix 붙이게 유도

**기각 이유**:
- LLM 프롬프트는 확률적. 준수 100% 보장 불가
- "비활성 세션 감지" 는 Claude 가 알 방법이 없음 (Claude 입장에선 자신이 active 인지 모름)
- 결정적 해결이 필요 (사용자의 핵심 불편: 세션 뒤섞임)

### 대안 4: 비활성 세션 reply 완전 차단 (drop)

**기각 이유**:
- Claude 가 작업한 결과물이 사라짐. 큰 UX 손실
- 사용자가 세션 전환한 의도는 "S2 와 대화" 지 "S1 답변 버림" 이 아님
- 지연 도착이라도 읽을 수 있어야 함

### 대안 5: reply 는 항상 활성 세션의 chat 으로 redirect

**기각 이유**:
- Claude 는 meta 의 chat_id 를 그대로 사용. redirect 하면 "다른 chat 의 답변이 지금 chat 에 옴" — 오히려 더 혼란
- 현재 allowlist 단일 chat 가정에서는 redirect 가 기술적으로 같은 chat 이긴 하지만, 확장성 (다중 chat) 고려 시 나쁨

## 오픈 이슈

- [ ] **`reply_sent` signal 의 미래**: `reply_response` 도입 후 계속 유지할지, deprecate 할지 결정 필요. 1차 구현 후 리뷰 시점에 재검토
- [ ] **`react` / `edit_message` 도 IPC 경유로 바꿀지**: 1차는 `reply` 만. 향후 일관성 고려해 확장 가능
- [ ] **버퍼 size 상한**: 현재 무제한. 실사용 데이터 쌓인 뒤 한계 정할지, 처음부터 넣을지
- [ ] **버퍼 TTL**: 너무 오래된 pending 은 자동 폐기해야 할 수 있음 (예: 1시간 이상)
- [ ] **pending pin 렌더링 시 세션 순서**: 현재는 Map iteration 순서 (삽입 순). id 순 / 버퍼 크기 순 정렬 원하면 별도 정의

## 리뷰 결과

리뷰 전. 구현 착수 전에 최소 1회 Skeptical + Architect 관점 검토 예정 (CLAUDE.md 제1원칙).
