# fixes.md

실제 적용된 diff 요약.

## 1. IPC 프로토콜 확장

**`src/core/ipc.ts`**:
```ts
export type IpcReplySent = {
  op: "reply_sent";
  session_id: string;
  message_ids?: number[];  // ← 신규 — Dispatcher 가 pin 하기 위해 필요
};
```

## 2. ToolHandler → McpResult 에 message_ids 전달

**`src/channels/telegram/tools/ToolHandler.ts`**:
```ts
export type McpResult = {
  content: Array<{ type: "text"; text: string }>;
  isError?: boolean;
  message_ids?: number[];  // ← 신규
};

private async handleReply(...): Promise<McpResult> {
  // ... 기존 chunk/send 로직 ...
  return {
    content: [{ type: "text", text: `sent message_ids=${sentIds.join(",")}` }],
    message_ids: sentIds,  // ← 신규
  };
}
```

## 3. server.ts → reply_sent 에 message_ids 포함

**`src/channels/telegram/server.ts`**:
```ts
if (isUserFacing && !result.isError && ipcMode && sessionId) {
  const payload = { op: "reply_sent", session_id: sessionId };
  if (result.message_ids && result.message_ids.length > 0) {
    payload.message_ids = result.message_ids;
  }
  ipcBridge.send(payload);
}
```

## 4. SlashHandler → /new label/cwd 보존

**`src/channels/telegram/SlashHandler.ts`**:
```ts
export class SlashHandler {
  private readonly pendingNewRequests = new Map<
    string,
    { label?: string; cwd?: string }
  >();

  // /new 받을 때:
  if (cmd.kind === "new") {
    const opts = {};
    if (cmd.label !== undefined) opts.label = cmd.label;
    if (cmd.cwd !== undefined) opts.cwd = cmd.cwd;
    this.pendingNewRequests.set(chatId, opts);
    // ... picker 표시 (preview 포함)
  }

  // 콜백 (new_normal / new_skip) 받을 때:
  if (action === "new_normal" || action === "new_skip") {
    const pending = this.pendingNewRequests.get(chatId) ?? {};
    this.pendingNewRequests.delete(chatId);
    const spawnOpts = { skipPermissions: action === "new_skip" };
    if (pending.label !== undefined) spawnOpts.label = pending.label;
    if (pending.cwd !== undefined) spawnOpts.cwd = pending.cwd;
    sessions.spawn(spawnOpts);
  }

  // cancel 시도 cleanup:
  if (action === "cancel") {
    this.pendingNewRequests.delete(chatId);
    // ...
  }
}
```

## 5. onActiveChanged 시그니처 — read-on-leave

**이전**: `onActiveChanged()` 가 새 active 의 count 를 리셋.
**이후**: `onActiveChanged(leftSessionId?)` — **떠나는 세션**의 count + pin 을 클리어.

```ts
// SlashHandler — 호출부
if (registry.active()?.id !== beforeActiveId) {
  this.deps.onActiveChanged(beforeActiveId);  // ← 이전 active 전달
}
```

## 6. Dispatcher — multi-pin + auto-switch

**`src/dispatcher.ts`**:
```ts
const pinnedReplyIds = new Map<string, number[]>();

async function pinInactiveReplies(sessionId, messageIds): Promise<void> {
  for (const mid of messageIds) {
    await tg.pinMessage(chat, mid, true);  // silent pin
    // ... track in pinnedReplyIds
  }
}

async function unpinRepliesOf(sessionId): Promise<void> {
  const ids = pinnedReplyIds.get(sessionId);
  pinnedReplyIds.delete(sessionId);
  await Promise.all(ids.map(mid => tg.unpinMessage(chat, mid)));
}

function onActiveChanged(leftSessionId?): void {
  if (leftSessionId) {
    pendingCount.set(leftSessionId, 0);
    void unpinRepliesOf(leftSessionId);
  }
  void doUpdateActivePin();
  pushSessionStateToAll();
}

// IPC hello 핸들러 — 자동 전환
case "hello": {
  const beforeActiveId = registry.active()?.id;
  const ready = handleIpcHello(...);
  if (ready?.kind === "spawned") {
    registry.setActive(ready.id);
    announce(`✅ [${ready.label}] 준비됨 — 자동 전환됨`);
    onActiveChanged(beforeActiveId);  // 이전 active 클리어
  } else if (ready?.kind === "reconnected") {
    announce(`🔄 [${ready.label}] 재연결됨`);
    pushSessionState(msg.session_id);
  } else {
    pushSessionState(msg.session_id);
  }
  break;
}

// reply_sent — 비활성 이면 pin
case "reply_sent": {
  if (session && active?.id !== session.id) {
    const count = msg.message_ids?.length ?? 1;
    pendingCount.set(session.id, (pendingCount.get(session.id) ?? 0) + count);
    if (msg.message_ids?.length) {
      void pinInactiveReplies(session.id, msg.message_ids);
    }
    void doUpdateActivePin();
  }
  onReplySent();
  break;
}

// Socket close — cleanup
ls.onClose(() => {
  // ... 기존 처리
  if (session) {
    pendingCount.delete(session.id);
    void unpinRepliesOf(session.id);  // ← 신규
    // ...
  }
});
```
