# fixes.md

> 설계 2 (Tag-only) 기준 diff 제안.

## 1. `src/core/ipc.ts` — `session_state` IPC op 추가

단방향 push. Dispatcher → MCP server 방향.

```ts
export type IpcSessionState = {
  op: "session_state";
  session_id: string;
  is_active: boolean;
  label: string;
};

// IpcMessage union 에 추가
export type IpcMessage =
  | IpcHello
  | IpcSignal
  | IpcInbound
  | IpcPermissionRequest
  | IpcPermissionReply
  | IpcReplySent
  | IpcSessionState;   // ← 신규
```

기존 메시지 타입은 변경 없음.

## 2. `src/channels/telegram/IpcBridge.ts` — 상태 캐시 + getter

```ts
export class IpcBridge {
  private socket: LineSocket | undefined;
  private closed = false;
  // ...
  private sessionState: { is_active: boolean; label: string } | undefined;

  get sessionActive(): boolean {
    return this.sessionState?.is_active ?? false;
  }

  get sessionLabel(): string {
    return this.sessionState?.label ?? "";
  }

  // onMessage 핸들러에 케이스 추가
  private async attach(): Promise<void> {
    // ...
    this.socket.onMessage((msg) => {
      switch (msg.op) {
        case "inbound": ...
        case "permission_reply": ...
        case "session_state":
          this.sessionState = {
            is_active: msg.is_active,
            label: msg.label,
          };
          break;
        default: ...
      }
    });
  }
}
```

**처음 연결 시 상태를 모르는 구간**: `sessionActive` 가 false 로 기본값. dispatcher 가 hello 수신 직후 session_state push 를 보내므로 실제로 "초기 모름" 기간은 수 ms 미만.

## 3. `src/channels/telegram/tools/ToolHandler.ts` — reply 시 prefix

```ts
export class ToolHandler {
  constructor(
    private readonly tg: TelegramClient,
    private readonly config: Config,
    private readonly ipcBridge: IpcBridge | undefined,  // 신규 파라미터
  ) {}

  private async handleReply(rawArgs: unknown): Promise<McpResult> {
    const args = replySchema.parse(rawArgs);
    assertAllowedChat(this.config, args.chat_id);

    // ── 비활성 세션이면 label prefix 주입 ─────────────────
    if (this.ipcBridge && !this.ipcBridge.sessionActive) {
      const label = this.ipcBridge.sessionLabel;
      if (label && args.text) {
        args.text = `[${label}] ${args.text}`;
      }
    }
    // ─────────────────────────────────────────────────

    // 이후 기존 chunking / sendMessage 로직 그대로
    const chunks = args.split === "length" ? chunkByLength(args.text) : chunkByNewline(args.text);
    // ...
  }

  // react / edit_message / download_attachment 는 변경 없음
  // react 는 message_id 기반이라 세션 혼동 없음
  // edit_message 도 기존 message_id 에 붙음
}
```

**파일 기반 reply**: `args.text` 가 빈 문자열이고 files 만 있는 케이스는 prefix 주입 안 함 (prefix 붙일 대상 없음). 파일 전송에 prefix 가 필요하면 후속 PR 에서.

## 4. `src/channels/telegram/server.ts` — ToolHandler 에 ipcBridge 주입

```ts
async function main(): Promise<void> {
  // ...
  const ipcBridge = new IpcBridge();
  if (ipcMode) {
    await ipcBridge.connect(dispatcherSocket!, sessionId!, process.pid, { ... });
  }

  // 기존: const toolHandler = new ToolHandler(tg, config);
  // 변경:
  const toolHandler = new ToolHandler(tg, config, ipcMode ? ipcBridge : undefined);
  // ...
}
```

**stand-alone 모드** (ipcMode=false): ipcBridge undefined → ToolHandler 에서 prefix 스킵 → 기존 동작 유지.

## 5. `src/dispatcher.ts` — session_state push + 카운터

```ts
// 상태
const pendingCount = new Map<string, number>();

function pushSessionState(sessionId: string): void {
  const session = registry.get(sessionId);
  if (!session) return;
  const socket = session.socketId ? sockets.get(session.socketId) : undefined;
  if (!socket) return;
  const active = registry.active();
  const isActive = active?.id === sessionId;
  socket.send({
    op: "session_state",
    session_id: sessionId,
    is_active: isActive,
    label: session.label,
  });
}

function pushSessionStateToAll(): void {
  for (const s of registry.list()) {
    pushSessionState(s.id);
  }
}

// IPC hello 수신 후
case "hello": {
  const ready = handleIpcHello(registry, msg.session_id, socketId);
  // ...
  pushSessionState(msg.session_id);  // ← 신규: 처음 연결 시 자기 상태 알려줌
  break;
}

// reply_sent 수신 시 — 카운터 증가
case "reply_sent": {
  const session = registry.getBySocketId(socketId);
  if (session) {
    const active = registry.active();
    if (active?.id !== session.id) {
      // 비활성 세션의 응답
      const cur = pendingCount.get(session.id) ?? 0;
      pendingCount.set(session.id, cur + 1);
      void doUpdateActivePin();
    }
  }
  onReplySent();  // 기존 애니메이션 종료 로직
  break;
}
```

**세션 전환 시** (`SlashHandler.onSessionAction("switch", ...)` 내부에서 dispatcher 의 콜백 호출):

```ts
function onActiveChanged(newActiveId: string | undefined): void {
  if (newActiveId) pendingCount.set(newActiveId, 0);
  void doUpdateActivePin();
  pushSessionStateToAll();  // 모든 세션에 새 active 상태 push
}
```

**세션 kill 시**: `pendingCount.delete(sessionId)` + `pushSessionStateToAll()`.

## 6. `src/core/pin.ts` — 카운터 포함 렌더

```ts
export async function updateActivePin(
  registry: Registry,
  tg: TelegramClient,
  chatId: string,
  pendingCounts?: Map<string, number>,
): Promise<void> {
  const active = registry.active();
  const lines: string[] = [];
  if (active) {
    lines.push(`🧷 active: ${active.id} (${active.label})`);
  } else {
    lines.push("🧷 no active session");
  }

  if (pendingCounts) {
    const pending = [...pendingCounts.entries()]
      .filter(([, n]) => n > 0)
      .sort(([a], [b]) => a.localeCompare(b));
    for (const [sid, count] of pending) {
      lines.push(`📬 ${sid}: ${count}`);
    }
  }

  const text = lines.join("\n");
  // 기존 pin 메시지 편집 또는 새로 pin
  // ...
}
```

Dispatcher 에서 호출 시 `pendingCount` 맵을 넘겨줌 (기존 호출부는 `undefined` 전달 가능 — backwards-compatible).

## 7. 순서 보장 (dispatcher 내부)

단일 JS 이벤트 루프에서 순차 처리되므로 race 가 거의 없음. 단 pin 편집 (`editMessageText`) 은 네트워크 왕복이라 여러 건 동시에 가면 순서 꼬일 수 있음 — `doUpdateActivePin` 을 serialize 하는 큐 하나 유지 권장 (선택).

```ts
let pinUpdateQueue: Promise<void> = Promise.resolve();
function doUpdateActivePin(): Promise<void> {
  pinUpdateQueue = pinUpdateQueue.then(() => updateActivePin(registry, tg, chatId, pendingCount));
  return pinUpdateQueue;
}
```

## 제거되는 것

이전 설계 (dispatcher-routed buffer) 에서 제안했던:
- ❌ `reply_request` / `reply_response` IPC op — **불필요**
- ❌ `IpcBridge.requestReply()` 메서드 — **불필요**
- ❌ `pendingBySession: Map<string, BufferedReply[]>` — **불필요**
- ❌ `executeReply()` in dispatcher — **불필요**
- ❌ `flushBufferForSession()` — **불필요**
- ❌ 세션 전환 시 버퍼 flush 로직 — **불필요**

변경 규모가 1/3 수준으로 축소됨.
