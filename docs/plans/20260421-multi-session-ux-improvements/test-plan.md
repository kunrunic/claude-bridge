# test-plan.md

## 신규 테스트 (6개 — 통과 확인)

### `tests/tool-handler.test.ts` (+2)

```ts
test("message_ids 배열이 McpResult 에 포함됨 (pin 용)")
test("chunked 메시지 → message_ids 배열 길이 = chunk 수")
```

### `tests/slash-handler.test.ts` (+4)

```ts
test("{kind:'new', label, cwd} → picker 에 label/cwd preview 표시, pending 저장")
test("/new label/cwd → new_normal 콜백에서 spawn 인자로 전달됨")
test("/new label → new_skip 콜백에서 skipPermissions=true 와 함께 전달")
test("/new → cancel 시 pending 지워짐 (다음 /new 에 누적 안 됨)")
```

## 회귀 테스트 매트릭스

기존 182 케이스 모두 통과. 민감 영역:

| 영역 | 회귀 위험 | 확인 방법 |
|---|---|---|
| ToolHandler.reply | `McpResult.message_ids` 필드 추가 → 기존 결과 문자열 검증 깨지지 않는지 | `tool-handler.test.ts` |
| SlashHandler.handle | /new 에 label/cwd 처리 경로 추가 | `slash-handler.test.ts` |
| SlashHandlerDeps | `doUpdateActivePin` → `onActiveChanged(leftSessionId?)` 시그니처 변경 | `slash-handler.test.ts` |
| Dispatcher IPC hello | 자동 전환 분기 추가 | 현재 dispatcher 직접 테스트 없음 — 수동 검증 |
| Dispatcher reply_sent | message_ids 기반 pin 호출 분기 추가 | 수동 검증 |
| Pin 모듈 | 시그니처 변경 없음 | 기존 `pin.test.ts` |

## 수동 시나리오 (재기동 후 검증)

1. **/new label cwd 전달**:
   - `/new mylabel ~/somedir` → 퍼미션 선택 시 picker 에 "(mylabel · ~/somedir)" 표시
   - 🔒 또는 🔴 클릭
   - 스폰 완료 후 `/sessions` 로 확인 → label=mylabel, cwd=~/somedir
   - 세션 내에서 "현재 폴더?" 질문 → `~/somedir` 응답

2. **자동 전환**:
   - s1 active 상태에서 `/new test` → 퍼미션 선택
   - "✅ [test] 준비됨 — 자동 전환됨" 메시지 수신
   - pin 이 `🧷 active: s2 (test)` 로 업데이트됨
   - 메시지 보내면 s2 로 감 (이전엔 s1 으로 갔음)

3. **Multi-pin 기본**:
   - s1, s2 스폰. s2 active.
   - s1 에 미리 질문 해두고 s2 로 전환한 상태를 만든 뒤 s1 Claude 응답 대기
   - s1 응답 오면 Telegram pin carousel 에 `[s1/label] ...` 메시지 pin 됨
   - 활성 pin (`🧷 active: s2`) 도 여전히 존재

4. **Read-on-leave**:
   - 위 상태에서 /sessions → s1 탭 → s1 active
   - **pin 은 유지됨** (아직 읽었다는 보장 없음)
   - pin 탭해서 메시지로 점프 → 읽음
   - /sessions → s2 로 다시 이동 → s1 의 pin 들 **자동 unpin**

5. **Session kill 시 unpin**:
   - s1 에 pending pin 있는 상태에서 /kill s1
   - s1 의 모든 pin 이 unpin 됨

6. **Socket 끊김 시 unpin**:
   - s1 Claude 프로세스 강제 종료 → IpcBridge onClose 발화
   - s1 의 pin cleanup

## 실행 게이트

```bash
bun run typecheck       # 0 에러
bun test                # 188 통과
bun tests/smoke-spawn.ts  # 기본 기동 회귀
bun tests/smoke-reply.ts  # reply 경로 (message_ids 포함됨 확인)
```

## 자동화 어려운 부분

- **Dispatcher IPC hello 자동 전환 로직**: dispatcher.ts 의 main loop 는 통합 테스트가 어려워 수동 검증에 의존. 선행 테스트 (handleIpcHello 가 kind="spawned" 반환) 는 이미 있음.
- **Multi-pin 라이프사이클**: Telegram API mock 이 복잡해서 수동 시나리오로 커버.
