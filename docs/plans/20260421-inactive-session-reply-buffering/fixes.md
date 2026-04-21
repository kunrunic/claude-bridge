# fixes.md

## 1. `src/core/ipc.ts` — IPC 프로토콜 확장

`reply_request` (MCP → dispatcher) + `reply_response` (dispatcher → MCP) 페어 추가. 기존 `reply_sent` 는 호환성 유지 (단순 신호용으로 계속 쓸 수도, 제거할 수도 있음 — 후속 결정).

### 제안 타입

```ts
export type IpcReplyRequest = {
  op: "reply_request";
  request_id: string;            // correlation id (uuid v4)
  session_id: string;
  tool: "reply" | "react" | "edit_message" | "download_attachment";
  args: Record<string, unknown>; // 툴별 원본 args (zod 검증은 이미 MCP 측에서 완료)
};

export type IpcReplyResponse = {
  op: "reply_response";
  request_id: string;
  status: "sent" | "buffered" | "error";
  message_id?: number;           // status=sent 시 Telegram message_id
  error?: string;                // status=error 시 사유
};
```

`IpcMessage` union 에 양쪽 추가. `LineSocket` 은 JSON line 프로토콜이라 구조 변경 없음.

## 2. `src/channels/telegram/tools/ToolHandler.ts` — 직접 전송 제거

현재:
```ts
private async handleReply(rawArgs: unknown): Promise<McpResult> {
  ...
  const id = await this.tg.sendMessage(args.chat_id, chunks[i]!, opts);  // 직접
  ...
}
```

변경:
```ts
private async handleReply(rawArgs: unknown): Promise<McpResult> {
  ...
  const response = await this.ipcBridge.requestReply({
    tool: "reply",
    args: parsedArgs,
  });
  if (response.status === "error") return mcpError(response.error);
  // response.status === "sent" or "buffered" 둘 다 Claude 입장에선 "성공"
  return mcpOk(response.message_id ?? null);
}
```

`ipcBridge.requestReply` 는 신규 메서드 — request_id 생성하고 response 대기 (Promise).

**주의**: `react` / `edit_message` / `download_attachment` 도 같이 IPC 경유로 전환할지 결정 필요. 
- `reply` 는 버퍼링 후보 (비활성 세션의 대화 응답)
- `react` / `edit_message` 는 **즉시 전송이 맞음** (사용자가 이미 보낸 메시지에 대한 반응이므로 활성 여부 무관)
- `download_attachment` 는 Telegram 전송 아니고 파일 다운로드 — 버퍼링 불필요

→ **1차 범위: `reply` 만 IPC 경유**. 나머지는 직접 전송 유지. IpcBridge 에 `requestReply` 만 추가.

## 3. `src/channels/telegram/IpcBridge.ts` — request/response 대기

```ts
private readonly pendingRequests = new Map<string, {
  resolve: (r: IpcReplyResponse) => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}>();

async requestReply(
  req: Omit<IpcReplyRequest, "op" | "request_id" | "session_id">,
): Promise<IpcReplyResponse> {
  if (!this.socket) throw new Error("IpcBridge not connected");
  const request_id = randomUUID();
  const full: IpcReplyRequest = {
    op: "reply_request",
    request_id,
    session_id: this.sessionId,
    ...req,
  };
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      this.pendingRequests.delete(request_id);
      reject(new Error("reply_request timeout"));
    }, 30_000);
    this.pendingRequests.set(request_id, { resolve, reject, timer });
    this.socket!.send(full);
  });
}
```

`onMessage` 핸들러에 `reply_response` 케이스 추가 → `pendingRequests` 에서 꺼내 resolve.

## 4. `src/dispatcher.ts` — 라우팅 + 버퍼

```ts
type BufferedReply = {
  request_id: string;
  tool: "reply";
  args: Record<string, unknown>;
  ts: number;
};

const pendingBySession = new Map<string, BufferedReply[]>();

// IPC 서버 핸들러 확장
ls.onMessage((msg: IpcMessage) => {
  switch (msg.op) {
    ...
    case "reply_request":
      handleReplyRequest(msg, ls);
      break;
  }
});

function handleReplyRequest(msg: IpcReplyRequest, ls: LineSocket): void {
  const session = registry.get(msg.session_id);
  if (!session) {
    ls.send({
      op: "reply_response", request_id: msg.request_id,
      status: "error", error: "session not found",
    });
    return;
  }
  const active = registry.active();
  const isActive = active?.id === session.id;

  if (isActive) {
    // 즉시 전송
    void executeReply(msg.args)
      .then((message_id) => ls.send({
        op: "reply_response", request_id: msg.request_id,
        status: "sent", message_id,
      }))
      .catch((err) => ls.send({
        op: "reply_response", request_id: msg.request_id,
        status: "error", error: String(err),
      }));
  } else {
    // 버퍼링
    const queue = pendingBySession.get(session.id) ?? [];
    queue.push({
      request_id: msg.request_id,
      tool: msg.tool,
      args: msg.args,
      ts: Date.now(),
    });
    pendingBySession.set(session.id, queue);
    void doUpdateActivePin();  // pin count 반영
    ls.send({
      op: "reply_response", request_id: msg.request_id,
      status: "buffered",
    });
  }
}

async function executeReply(args: Record<string, unknown>): Promise<number | undefined> {
  // tg.sendMessage 호출 — 실제 전송 로직은 ToolHandler 에서 뜯어옴
  // (중복 방지 위해 별도 함수로 분리 예정)
}

async function flushBufferForSession(sessionId: string): Promise<void> {
  const queue = pendingBySession.get(sessionId);
  if (!queue || queue.length === 0) return;
  const session = registry.get(sessionId);
  if (!session) return;
  for (const buf of queue) {
    // label prefix 주입
    const prefixed = { ...buf.args };
    if (typeof prefixed.text === "string") {
      prefixed.text = `[${session.label}] ${prefixed.text}`;
    }
    await executeReply(prefixed).catch((err) => {
      anomaly.log("channel_reply_failed", {
        op: "flush_buffer", sessionId, error: String(err),
      });
    });
  }
  pendingBySession.delete(sessionId);
  void doUpdateActivePin();
}
```

## 5. `src/core/pin.ts` — 세션별 count 표시

현재 `updateActivePin` 은 활성 세션 정보만 pin 에 올림. 버퍼 count 를 받아서 같이 렌더.

```ts
export async function updateActivePin(
  registry: Registry,
  tg: TelegramClient,
  chatId: string,
  pendingCounts?: Map<string, number>,
): Promise<void> {
  const active = registry.active();
  const lines: string[] = [];
  if (active) lines.push(`🧷 active: ${active.id} (${active.label})`);
  else lines.push("🧷 no active session");

  if (pendingCounts && pendingCounts.size > 0) {
    for (const [sid, count] of pendingCounts) {
      if (count === 0) continue;
      const s = registry.get(sid);
      const label = s?.label ?? sid;
      lines.push(`📬 ${sid}: ${count}`);
    }
  }

  const text = lines.join("\n");
  // 기존 pin 로직 (sendMessage + pinChatMessage 또는 editMessageText)
}
```

dispatcher 에서 호출 시 `pendingCounts` 를 `pendingBySession` 에서 파생해서 전달:
```ts
const counts = new Map<string, number>();
for (const [sid, queue] of pendingBySession) counts.set(sid, queue.length);
await updateActivePin(registry, tg, chatId, counts);
```

## 6. `src/channels/telegram/SlashHandler.ts` — 전환 시 flush

세션 전환 콜백(`onSessionAction` switch 케이스)에서 flush 호출 추가:

```ts
case "switch": {
  const s = registry.get(target) ?? registry.getByLabel(target);
  if (!s) { ...; return; }
  registry.setActive(s.id);
  await flushBufferForSession(s.id);   // ← 신규
  await this.deps.doUpdateActivePin();
  ...
}
```

`flushBufferForSession` 는 dispatcher 가 소유하므로 SlashHandlerDeps 에 콜백 추가.

## 7. 세션 kill 시 버퍼 처리

`/kill` 로 세션 종료되거나 `handleIpcSignal` 로 dead 상태 되면 해당 세션의 pending 버퍼는 **discard** 가 맞음 (세션이 사라졌으니 복구 불가). dispatcher.ts 에서 kill 경로 찾아 `pendingBySession.delete(sessionId)` 호출.

## 8. Dispatcher 재기동 시

`reconcileOrphans` 가 registry 를 비우고 재시작하는 기존 철학 유지. 버퍼는 메모리에 있었으니 자동 소실 — 별도 처리 불필요.
