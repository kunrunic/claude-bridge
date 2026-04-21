# test-plan.md

> 설계 2 (Tag-only) 기준.

## 신규 테스트

### `tests/session-state-push.test.ts` (신규)

Dispatcher 의 session_state push 타이밍 검증.

```ts
describe("session_state push", () => {
  test("hello 수신 직후 해당 세션에 session_state push", async () => {
    // fake registry + 가짜 socket
    // hello 메시지 송신
    // expect: 이어서 session_state 메시지가 해당 socket 으로 send 됨
    // expect: is_active = true (첫 세션이라 자동 활성)
  });

  test("switch 시 기존/새 active 모두 push", async () => {
    // s1, s2 연결 상태, s1 이 active
    // switch s2
    // expect: s1 socket 에 session_state { is_active: false }
    // expect: s2 socket 에 session_state { is_active: true }
  });

  test("kill 시 남은 세션들에 push", async () => {
    // s1 active, s2 inactive
    // kill s1
    // expect: s2 socket 에 session_state { is_active: ? } (active 가 바뀌었으면 반영)
  });
});
```

### `tests/ipc-bridge.test.ts` (확장)

```ts
test("session_state 수신 → sessionActive / sessionLabel getter 반영", async () => {
  // bridge 연결
  // 서버가 session_state { is_active: false, label: "backend" } 송신
  // expect: bridge.sessionActive === false
  // expect: bridge.sessionLabel === "backend"
});

test("session_state 이전엔 getter 기본값 false/빈문자열", () => {
  // 새 bridge 인스턴스
  // expect: bridge.sessionActive === false
  // expect: bridge.sessionLabel === ""
});
```

### `tests/tool-handler.test.ts` (확장)

```ts
test("handleReply: 활성 세션이면 prefix 없음", async () => {
  const fakeBridge = { sessionActive: true, sessionLabel: "x" };
  const handler = new ToolHandler(tg, config, fakeBridge);
  await handler.handle("reply", { chat_id: "c1", text: "hello" });
  // expect: tg.sendMessage 호출된 text 가 "hello" (prefix 없음)
});

test("handleReply: 비활성 세션이면 [label] prefix", async () => {
  const fakeBridge = { sessionActive: false, sessionLabel: "backend" };
  const handler = new ToolHandler(tg, config, fakeBridge);
  await handler.handle("reply", { chat_id: "c1", text: "done" });
  // expect: tg.sendMessage 호출된 text 가 "[backend] done"
});

test("handleReply: ipcBridge undefined (stand-alone) 시 prefix 없음", async () => {
  const handler = new ToolHandler(tg, config, undefined);
  await handler.handle("reply", { chat_id: "c1", text: "hello" });
  // expect: "hello" 그대로
});

test("handleReply: text 비어있고 files 만 있으면 prefix 스킵", async () => {
  const fakeBridge = { sessionActive: false, sessionLabel: "backend" };
  const handler = new ToolHandler(tg, config, fakeBridge);
  await handler.handle("reply", { chat_id: "c1", text: "", files: ["/tmp/a.jpg"] });
  // expect: text 는 빈 문자열 유지 (prefix 주입 안 함)
});

test("react / edit_message 는 prefix 영향 없음", async () => {
  // react 호출 — 내부 tg.setReaction 만 호출됨. text 개념 없음.
  // edit_message — 기존 text 가 그대로. 세션 prefix 붙지 않음 (의도)
});
```

### `tests/pin.test.ts` (확장)

```ts
test("pin 텍스트에 pending count 포함", () => {
  // active: s1, pending: s2=2, s3=1
  const out = renderPin(registry, counts);
  expect(out).toContain("🧷 active: s1");
  expect(out).toContain("📬 s2: 2");
  expect(out).toContain("📬 s3: 1");
});

test("pending count 0 인 세션은 표시 안 함", () => {
  const counts = new Map([["s1", 0]]);
  const out = renderPin(registry, counts);
  expect(out).not.toContain("📬");
});

test("pendingCounts undefined 시 pending 섹션 생략", () => {
  const out = renderPin(registry);
  expect(out).not.toContain("📬");
});
```

### `tests/dispatcher-reply-counter.test.ts` (신규)

Dispatcher 의 reply_sent 처리 + 카운터.

```ts
describe("reply_sent counter", () => {
  test("active 세션의 reply_sent → 카운터 증가 없음", () => {
    // s1 active, s1 에서 reply_sent 수신
    // expect: pendingCount.get("s1") === undefined or 0
  });

  test("inactive 세션의 reply_sent → 카운터 +1", () => {
    // s1 active, s2 에서 reply_sent 수신
    // expect: pendingCount.get("s2") === 1
  });

  test("세션 전환 시 새 active 의 카운터 리셋", () => {
    // s2 카운터 3, switch to s2
    // expect: pendingCount.get("s2") === 0
  });

  test("kill 시 해당 세션 카운터 제거", () => {
    // s2 카운터 2
    // kill s2
    // expect: pendingCount.has("s2") === false
  });
});
```

## 회귀 테스트 매트릭스

기존 171 케이스 모두 통과. 민감 영역:

| 영역 | 회귀 위험 | 확인 방법 |
|---|---|---|
| ToolHandler | `handleReply` 시그니처 (ipcBridge 파라미터 추가) | 기존 `tool-handler.test.ts` |
| IpcBridge | onMessage 분기 확장 | 기존 `ipc-bridge.test.ts` |
| Dispatcher IPC | hello / reply_sent 경로 추가 로직 | 기존 `dispatcher-handlers.test.ts` |
| Pin 렌더 | 시그니처 변경 (optional 카운터 인자 추가) | 기존 `pin.test.ts` |
| SlashHandler | switch 시 카운터 리셋 훅 추가 | 기존 `slash-handler.test.ts` |

## 스모크 테스트

### `bun tests/smoke-spawn.ts` (기존)
변경 없음.

### `bun tests/smoke-reply.ts` (기존)
**핵심 회귀 게이트** — 이번 변경이 reply 경로에 영향 주므로 반드시 통과. 단 세션 1개 시나리오만 커버.

### 수동 시나리오 (side-effects.md 매트릭스 참조)
자동화 없이 수동 검증 필수:
- 2개 세션 스폰 → S1 에 메시지 → S2 로 전환 → S1 Claude reply → Telegram 에 `[s1/label] ...` prefix 도달
- Pin 에 `📬 s1: 1` 표시 → S1 탭 전환 → 카운터 사라짐

## 실행 게이트

```bash
bun run typecheck                       # 0 에러
bun test                                # 171 + 신규 테스트 모두 통과
bun tests/smoke-spawn.ts                # 기본 기동 검증
bun tests/smoke-reply.ts                # 핵심 회귀 (이번 변경 주 영향)
```

수동 시나리오는 side-effects.md 의 7개 케이스 중 최소 3개 통과 확인.
