# 비활성 세션 reply 버퍼링 + pin 세션별 count

## 배경

현재 `/sessions` 로 활성 세션을 전환해도, 이전 세션의 Claude 가 작업을 마치고 `reply` 도구를 호출하면 **그 메시지가 현재 활성 세션과의 대화 흐름에 그대로 끼어든다**. 사용자는:

- 어떤 세션에서 온 답변인지 식별 불가 (label 표시 없음)
- 지금 S2와 대화 중인데 갑자기 S1 의 답이 섞여 혼란
- 여러 세션 병렬 운영의 UX 가치가 반감됨

## 현재 동작 (AS-IS)

```
[Claude S1] → [MCP server (S1 subprocess)] → [Telegram] (직접 전송)
                      ↓ IPC
                 [dispatcher]  (reply_sent 신호만 수신)
```

- MCP 서버가 자체 `TelegramClient` 로 직접 전송 (`src/channels/telegram/tools/ToolHandler.ts:113`)
- dispatcher 는 `reply_sent` IPC 신호만 받음 (애니메이션 종료용)
- **비활성 세션 reply 를 가로챌 지점이 없음**

## 제안 변경 (TO-BE)

### 라우팅 전환: reply 를 dispatcher 경유로

```
[Claude S1] → [MCP server] → [IPC: reply_request] → [dispatcher 라우팅]
                                                            ↓
                                              active  → Telegram 즉시 전송
                                              inactive → memory 버퍼 + pin count +1
```

### Pin 카운터 (활성 표시 + 대기 카운트)

```
🧷 active: s2 (backend)
📬 s1: 2
📬 s3: 1
```

- 세션별 pending count 만 표시 (내용 preview 없음 — 모바일 앱 badge 스타일)
- 전환 시 해당 세션 카운트 0 으로, 버퍼 flush

### 버퍼 flush 시 라벨 prefix

전환 시 메모리 버퍼의 메시지들을 순서대로 Telegram 에 전송할 때 `[s1/backend] ...` 형태로 세션 라벨 prefix 를 붙여 **어떤 세션에서 온 답변인지 식별 가능하게**.

### 저장소 선택: Memory

- `Map<sessionId, BufferedReply[]>` 메모리 저장
- 재기동 시 **유실 허용** — claude-bridge 는 재기동 시 세션 자체를 리셋하는 철학 (reconcileOrphans) 과 일관성 유지
- SQLite 는 오버엔지니어링 (side-effects.md 의 "기각된 대안" 참조)

## 변경 파일 범위

| 파일 | 변경 내용 |
|---|---|
| `src/core/ipc.ts` | `reply_request` / `reply_response` IPC op 추가 |
| `src/channels/telegram/tools/ToolHandler.ts` | `handleReply` 직접 전송 → IPC 위임으로 변경 |
| `src/channels/telegram/IpcBridge.ts` | reply_request 송신 + response 대기 로직 |
| `src/dispatcher.ts` | reply_request 핸들러, 메모리 버퍼, 전환 시 flush |
| `src/core/pin.ts` | pin 텍스트에 세션별 pending count 추가 |
| `src/channels/telegram/SlashHandler.ts` | 세션 전환 시 버퍼 flush 트리거 |
| `tests/` | 신규 테스트 다수 (test-plan.md 참조) |

## 체크리스트

- [ ] `reply_request` IPC 프로토콜 정의 (request/response 페어)
- [ ] ToolHandler 의 모든 전송 tool (`reply`, `react`, `edit_message`) IPC 경유로 전환 검토
- [ ] dispatcher 메모리 버퍼 구현 + 세션 전환 시 flush
- [ ] Pin 카운터 포맷 업데이트
- [ ] 세션 kill 시 버퍼 처리 (discard vs preserve)
- [ ] 신규 테스트 (test-plan.md)
- [ ] 회귀 테스트 (현재 177 개) 통과
- [ ] `bun tests/smoke-spawn.ts` 통과
- [ ] 수동 시나리오 검증 (side-effects.md 의 시나리오 매트릭스)

## 참고

- 변경 diff 상세: [fixes.md](fixes.md)
- 테스트 계획: [test-plan.md](test-plan.md)
- 리스크 / 기각 대안: [side-effects.md](side-effects.md)
- AS-IS 세션 생명주기: [../../design/03-session-lifecycle.md](../../design/03-session-lifecycle.md)
- AS-IS MCP 채널 프로토콜: [../../design/02-mcp-channel-protocol.md](../../design/02-mcp-channel-protocol.md)
