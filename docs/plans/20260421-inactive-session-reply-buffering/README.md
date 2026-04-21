# 비활성 세션 reply 식별 (Tag-only) + pin 카운터

> **설계 버전 2** — 2026-04-21 리뷰 결과 반영
> 이전 버전 (dispatcher-routed buffer) 은 `side-effects.md` 의 "기각된 대안" 참조

## 배경

`/sessions` 로 활성 세션을 전환해도, 이전 세션의 Claude 가 작업을 마치고 `reply` 도구를 호출하면 그 메시지가 현재 활성 세션 대화 흐름에 라벨 없이 섞여 들어온다. 사용자는:

- 어느 세션의 답변인지 식별 불가
- S2 와 대화 중인데 S1 답변이 섞여 혼란
- 멀티 세션 병렬 운영의 UX 가치 반감

## 현재 동작 (AS-IS)

```
[Claude S1] → [MCP server (s1)] → [tg.sendMessage]  (직접 Telegram 전송)
                    ↓ IPC
              [dispatcher]  (reply_sent 신호만 수신 — 애니메이션 종료용)
```

- MCP 서버가 `TelegramClient` 로 **직접 전송**
- dispatcher 는 세션이 inactive 였는지 알아도 메시지 내용/전달 경로를 건드리지 않음

## 제안 변경 (TO-BE)

### 핵심 아이디어

**Dispatcher 가 MCP 서버에게 "너는 지금 active/inactive 다" 를 IPC 로 push** 하면, MCP 서버가 스스로 **자기 세션 라벨을 메시지 text 앞에 prefix 로 붙여서** 직접 전송한다. 메시지 라우팅이나 버퍼링은 하지 않는다.

### 동작

```
[dispatcher] ──[session_state push]──→ [MCP server (s1)]
                                             ↓ (캐시: isActive=false, label="backend")
[Claude S1] ─────[reply tool 호출]──────→ [ToolHandler]
                                             ↓ (isActive=false 이므로)
                                        text = "[s1/backend] " + 원문
                                             ↓
                                        [tg.sendMessage]  (직접 전송, 기존 경로)

[MCP server] ──[reply_sent IPC]──→ [dispatcher]
                                         ↓ (세션이 inactive 였으므로)
                                    pendingCount[s1] += 1
                                         ↓
                                    pin update: "active: s2 | 📬 s1: 1"

[user tap s1] → switch → pendingCount[s1] = 0 → pin update
                      → session_state push to s1 (isActive=true)
                      → session_state push to s2 (isActive=false)
```

### 무엇이 달라지는가

- **Reply 는 여전히 MCP server 가 직접 전송** — 라우팅/버퍼링 없음
- MCP server 는 자기 상태를 알고, **prefix 주입만** 수행
- Dispatcher 는 **상태 push 와 카운터 관리만** 담당
- 기존 `reply_sent` IPC 시그널 재활용 (새 프로토콜 최소)

### Pin 표시

```
🧷 active: s2 (backend)
📬 s1: 2
📬 s3: 1
```

- 세션별 inactive reply 카운트만 표시
- 전환 시 해당 세션 카운트 0 으로 리셋
- 내용 preview 없음 (모바일 앱 badge 스타일)

## 변경 파일 범위

| 파일 | 변경 내용 |
|---|---|
| `src/core/ipc.ts` | `session_state` IPC op 추가 (dispatcher → MCP) |
| `src/channels/telegram/IpcBridge.ts` | `session_state` 수신 핸들링, 캐시 (isActive, label) 노출 |
| `src/channels/telegram/tools/ToolHandler.ts` | `handleReply` 가 inactive 상태일 때 text prefix |
| `src/channels/telegram/server.ts` | ToolHandler 에 sessionState getter 주입 |
| `src/dispatcher.ts` | hello 수신 / switch 시 session_state push, reply_sent 시 카운터 증가 |
| `src/core/pin.ts` | pin 렌더에 카운터 표시 추가 |
| `tests/` | 신규 테스트 (test-plan.md) |

**기존 설계 대비 줄어든 변경**: reply_request / reply_response IPC 페어, dispatcher 의 executeReply, 메모리 버퍼 Map, 세션 전환 시 flush 로직 — 전부 **불필요**.

## 체크리스트

- [ ] `session_state` IPC 프로토콜 정의 (단방향 push)
- [ ] IpcBridge 가 상태 캐시 + getter 노출
- [ ] ToolHandler 가 sessionState 기반 prefix (text 필드만, files/react/edit 무관)
- [ ] Dispatcher 가 hello / switch / kill 시 push 정확히
- [ ] pendingCount Map + pin 렌더 확장
- [ ] 세션 전환 시 카운터 리셋 (switch, pin 동시 업데이트)
- [ ] 세션 kill 시 카운터/상태 cleanup
- [ ] 신규 테스트 (test-plan.md)
- [ ] 회귀 테스트 (현재 171 개) 통과
- [ ] `bun tests/smoke-reply.ts` 통과 — 활성 세션 기본 flow 검증

## 참고

- 상세 diff: [fixes.md](fixes.md)
- 테스트 계획: [test-plan.md](test-plan.md)
- 리스크 / 기각 대안: [side-effects.md](side-effects.md)
- 리뷰 결과 반영 근거: `side-effects.md` 의 "리뷰 결과" 섹션
