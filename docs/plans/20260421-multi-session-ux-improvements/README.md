# 멀티 세션 UX 개선 (3 건 묶음)

> 실제 사용 중 발견된 3가지 이슈를 하나의 변경으로 묶어 해결.
> 상태: **구현 완료** — 188 테스트 통과.

## 배경

`20260421-inactive-session-reply-buffering` (Tag-only) 반영 후 실제 사용 중 관찰:

1. `/new test ~/cb_test` 실행해도 label/cwd 무시되고 기본값으로 스폰됨
2. `/new` 성공 후 "준비됨" 알림만 오고 active 는 여전히 이전 세션 — 사용자가 보낸 메시지가 잘못된 세션으로 감
3. 비활성 세션 reply 가 prefix 붙어 채팅에 섞여 있지만 **스크롤해서 찾아야** 읽을 수 있음. Pin 카운터는 있지만 실제 메시지로 점프 불가

## 수정 내용

### 1. `/new label cwd` 버그 수정

**원인**: `/new` 가 퍼미션 picker 인라인 키보드를 띄울 때 `cmd.label` / `cmd.cwd` 가 사라짐. 이후 `new_normal:` 콜백은 기본값으로만 spawn.

**수정**: `SlashHandler` 에 `pendingNewRequests: Map<chatId, {label?, cwd?}>` 추가. /new 받을 때 저장, 콜백 시 꺼내서 spawn 인자에 포함. cancel 시 삭제.

Picker 메시지에 `(label · cwd)` preview 도 같이 표시 — 사용자가 뭐 들어갈지 확인 후 퍼미션 선택.

### 2. `/new` 후 자동 전환

**수정**: Dispatcher 의 IPC hello 핸들러에서 `kind: "spawned"` 일 때 `registry.setActive(ready.id)` 자동 호출 + `onActiveChanged(beforeActiveId)` 호출.

새 세션이 hello 보내서 "준비됨" 상태가 되면 즉시 active 로 전환됨. 안내 메시지도 `✅ [s2] 준비됨 — 자동 전환됨` 으로 명확화.

### 3. Multi-pin (실제 메시지 pin) + Read-on-leave 시멘틱

**변경 포인트**:
- **IPC 확장**: `IpcReplySent` 에 `message_ids?: number[]` 추가. MCP server 가 전송한 Telegram 메시지 ID 를 dispatcher 에 전달.
- **Dispatcher 가 비활성 reply 메시지를 직접 pin**: `reply_sent` 수신 시 active 가 아니면 각 message_id 를 `pinMessage` 로 pin. 세션별 `pinnedReplyIds: Map<sessionId, number[]>` 로 추적.
- **Read-on-leave**: 기존 `onActiveChanged()` 가 새 active 의 count 를 리셋하던 로직을 **이전 active (leftSessionId) 의 count + pin 을 클리어** 하는 방식으로 변경. 사용자가 세션을 **떠날 때** 읽은 걸로 간주.
- **Session kill / socket close 시**: pin 메시지 일괄 unpin + count cleanup.

**결과 UX**:
```
1. S1 에 질문 → S2 전환 → S1 Claude 응답
2. Telegram 에 [s1/label] 답변 즉시 도착 + 그 메시지가 pinned
3. Pin carousel 에 쌓이므로 탭해서 점프 가능
4. S1 으로 전환 (pin 유지됨 — 아직 안 읽었을 수 있으니)
5. 사용자가 pin 탭하며 읽음
6. 다른 세션으로 이동 → S1 의 pin 들 자동 unpin ("다 봤다" 간주)
```

## 변경 파일

| 파일 | 변경 |
|---|---|
| `src/core/ipc.ts` | `IpcReplySent.message_ids?` 추가 |
| `src/channels/telegram/tools/ToolHandler.ts` | `McpResult.message_ids?` 추가, `handleReply` 결과에 포함 |
| `src/channels/telegram/server.ts` | `reply_sent` IPC 에 `message_ids` 포함해서 전송 |
| `src/channels/telegram/SlashHandler.ts` | `pendingNewRequests` Map + 콜백에서 복원, `onActiveChanged(leftSessionId)` 시그니처 변경, picker 메시지에 label/cwd preview |
| `src/dispatcher.ts` | `pinnedReplyIds` Map, `pinInactiveReplies` / `unpinRepliesOf` 함수, hello kind=spawned 자동 전환, read-on-leave `onActiveChanged(leftSessionId)` |
| `tests/tool-handler.test.ts` | `message_ids` 반환 테스트 2개 추가 |
| `tests/slash-handler.test.ts` | `/new label/cwd` preservation + cancel cleanup 테스트 4개 추가 |

## 체크리스트

- [x] `/new label cwd` 전달 확인
- [x] 자동 전환 시 `pushSessionStateToAll()` 로 prefix 정합성 유지
- [x] Read-on-leave 로직 변경 — 이전 active 의 count/pin 클리어
- [x] Multi-pin: `pinMessage(silent=true)` 로 무음 pin
- [x] Session kill 시 unpin 처리
- [x] Socket close 시 unpin 처리
- [x] 188 테스트 통과 (기존 182 + 신규 6)
- [x] 타입체크 통과

## 수동 검증 필요

- `/new mylabel ~/somedir` → 실제로 `mylabel` 라벨 + `~/somedir` cwd 로 스폰되는지
- `/new` 후 메시지 보내면 새 세션으로 가는지 (자동 전환 확인)
- S1 → S2 전환 후 S1 reply 옴 → Telegram pin carousel 에 메시지 pin 되는지
- S1 으로 전환 → 다른 세션으로 이동 → S1 pin 자동 unpin 되는지

## 참고

- 상세 diff: [fixes.md](fixes.md)
- 테스트 계획: [test-plan.md](test-plan.md)
- 리스크 / 기각 대안: [side-effects.md](side-effects.md)
- 선행 plan: [../20260421-inactive-session-reply-buffering/README.md](../20260421-inactive-session-reply-buffering/README.md)
