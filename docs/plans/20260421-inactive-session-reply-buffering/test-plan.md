# test-plan.md

## 신규 테스트

### `tests/reply-buffering.test.ts` (신규 파일)

dispatcher 의 라우팅 로직을 격리 테스트.

```ts
describe("reply routing", () => {
  test("active session reply → 즉시 전송", async () => {
    // fake registry: s1 active
    // simulate reply_request for s1
    // expect: tg.sendMessage 호출됨, status=sent, message_id 반환
  });

  test("inactive session reply → 버퍼링", async () => {
    // fake registry: s1 active, request for s2
    // simulate reply_request for s2
    // expect: tg.sendMessage 호출 없음, status=buffered
    // expect: pendingBySession.get("s2").length === 1
  });

  test("여러 inactive reply 누적 → count 증가", async () => {
    // request_id A, B, C 연속 호출 (s2 대상, s1 active)
    // expect: pendingBySession.get("s2").length === 3
  });

  test("세션 전환 시 버퍼 flush + label prefix", async () => {
    // s2 에 2개 버퍼 상태
    // switchActive("s2") 트리거
    // expect: tg.sendMessage 2회 호출
    // expect: 두 호출의 text 가 "[label] ..." prefix 포함
    // expect: pendingBySession.get("s2") undefined
  });

  test("kill 시 버퍼 discard", async () => {
    // s2 에 버퍼 3개
    // killSession("s2")
    // expect: pendingBySession.get("s2") undefined (세션과 함께 삭제)
    // expect: tg.sendMessage 호출 없음
  });

  test("세션 없음 → error response", async () => {
    // reply_request for 존재 안 하는 session_id
    // expect: status=error, error="session not found"
  });
});
```

### `tests/ipc-bridge.test.ts` (기존 확장)

```ts
test("requestReply — timeout 후 reject", async () => {
  // 서버가 response 안 보냄
  // expect: 30s 대기 후 reject "reply_request timeout"
});

test("requestReply — response 도착 시 resolve", async () => {
  // 서버가 reply_response 로 status=sent 회신
  // expect: resolve 되고 response.message_id 전달됨
});

test("여러 request 병렬 — request_id 로 구분", async () => {
  // A, B 동시 요청, 서버가 B → A 순으로 응답
  // expect: 각각 올바른 resolve
});
```

### `tests/pin.test.ts` (기존 확장)

```ts
test("pin 텍스트에 pending count 포함", () => {
  // active: s1, pending: s2=2, s3=1
  // expect: "🧷 active: s1 ..." + "📬 s2: 2" + "📬 s3: 1" 포함
});

test("pending count 0 인 세션은 표시 안 함", () => {
  // active: s1, pending: {} (빈 맵)
  // expect: 📬 라인 없음
});
```

## 회귀 테스트 매트릭스

기존 177 케이스 모두 통과 필수. 특히 아래 민감 영역:

| 영역 | 회귀 위험 | 확인 방법 |
|---|---|---|
| MCP ToolHandler | `handleReply` 시그니처 변경 | 기존 `tool-handler.test.ts` 실행 |
| IpcBridge | `onMessage` 분기 추가 | 기존 `ipc-bridge.test.ts` 실행 |
| dispatcher IPC 서버 | 신규 op 케이스 추가 | 기존 `dispatcher-handlers.test.ts` |
| Pin 렌더링 | 시그니처 변경 (인자 추가) | 기존 `pin.test.ts` |
| SlashHandler | switch 케이스 동작 변경 | 기존 `slash-handler.test.ts` |

## 스모크 테스트

### `bun tests/smoke-spawn.ts` (기존)
변경 없음. 스폰 → IPC hello 왕복만 검증. reply 경로는 touch 안 함.

### `bun tests/smoke-reply.ts` (기존)
**주의**: Claude → reply() → Telegram 경로 전체 검증. 이번 변경으로 중간에 dispatcher 경유가 들어가므로 **이 테스트가 실질적 통합 검증**. 반드시 통과 확인.

### `bun tests/smoke-multi-session-buffer.ts` (신규, 선택)
실제 2개 세션 스폰 → S1에 메시지 → S2로 전환 → S1 Claude가 reply → 버퍼 확인 → S1 전환 → flush 검증.

수동 재현 대안: 스모크 자동화가 복잡하면 [side-effects.md](side-effects.md) 의 "수동 시나리오 매트릭스" 로 대체.

## 실행 게이트

```bash
bun run typecheck                       # 0 에러
bun test                                # 177 + 신규 테스트 모두 통과
bun tests/smoke-spawn.ts                # 기본 기동 검증
bun tests/smoke-reply.ts                # 핵심 회귀 검증 (이번 변경 주 영향)
```

스모크 수동 시나리오 (side-effects.md) 도 최소 3개 케이스 통과 확인 후 PR 올린다.
