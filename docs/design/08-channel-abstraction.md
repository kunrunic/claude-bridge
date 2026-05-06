# 08. 채널 추상화

dispatcher 와 외부 매체(Telegram, tmux, cb-menu 등) 사이의 경계.
같은 인터페이스 위에 여러 채널이 동시 동작하며, dispatcher 본체는 각 채널의 구현 상세를 알지 못한다.

---

## 왜 추상화인가

- **여러 채널 동시 운영** — Telegram 봇이 켜진 상태에서 cb CLI 도 같이 쓸 수 있어야 한다. 둘 다 같은 SessionEvent 를 받아 자기 매체에 맞게 표현
- **신규 채널 추가 비용 제로** — Slack, Discord, 웹 UI 같은 새 매체는 `src/channels/<name>/` 에 Channel 구현 하나만 추가. core 변경 없이
- **테스트 가능성** — dispatcher 로직을 mock channel 로 검증

추상화 위치: `src/core/channel.ts`. 이 파일이 dispatcher 와 채널들의 공유 계약.

---

## Channel 인터페이스

```typescript
export interface Channel {
  readonly name: string;

  start(): Promise<void>;
  stop(): Promise<void>;

  /** 세션 단위 의미 이벤트. 채널이 자기 방식으로 렌더링. */
  notify(evt: SessionEvent): void;

  /** 시스템 단위 안내 텍스트(예: "🟢 claude-bridge started"). 세션과 무관. */
  announce(text: string): void;

  /** 권한 요청 UI 표시. 사용자 응답은 ChannelDeps.onPermissionReply 로 회신. */
  requestPermission(req: PermissionRequest): void;
}
```

세 가지 출력만 — `notify` (세션 이벤트), `announce` (시스템 메시지), `requestPermission` (권한 UI).
입력은 생성자 시점에 받은 콜백(`ChannelDeps`) 으로 dispatcher 에 통보.

---

## SessionEvent

dispatcher 가 모든 채널에 fan-out 하는 의미 이벤트:

```typescript
export type SessionEvent =
  | ({ type: "spawned"; resumed: boolean; autoSwitched: boolean } & SessionRef)
  | ({ type: "killed" } & SessionRef)
  | ({ type: "active_changed"; previousId?: string } & SessionRef)
  | { type: "active_cleared"; previousId?: string }
  | ({ type: "state_changed"; state: SessionState; signal: Signal } & SessionRef)
  | ({ type: "inbound_delivered"; meta?: ChannelInboundMeta } & SessionRef)
  | ({ type: "reply_sent"; messageIds?: number[] } & SessionRef)
  | ({ type: "disconnected"; sshSession?: boolean } & SessionRef)
  | ({ type: "reconnected" } & SessionRef);
```

`SessionRef` = `{ sessionId: string; label: string }`.

각 이벤트가 채널마다 다르게 표현됨:

| 이벤트 | TelegramChannel | TmuxStatusChannel | (가상) SlackChannel |
|---|---|---|---|
| `spawned` | "🟢 spawned [s1] my-app" announce | minimap 갱신 | 채널 메시지 + emoji |
| `active_changed` | pin 갱신 | minimap 의 ▶ 위치 변경 | thread 마크 |
| `state_changed` | (보통 무시 — UX 부담) | minimap sigil 변경 | (생략 가능) |
| `inbound_delivered` | ✍ reaction + 애니메이션 시작 | (무시) | typing indicator |
| `reply_sent` | 애니메이션 종료 → ✅ | (무시) | typing 해제 |
| `reconnected` | 🔄 알림 | (무시) | thread 재연결 메시지 |

채널이 이벤트를 어떻게 표현할지는 자유. 의미 없는 이벤트는 무시.

---

## InboundEvent / PermissionReply (채널 → dispatcher)

채널이 사용자 입력을 받아 dispatcher 에 올리는 두 가지 형태:

### InboundEvent

```typescript
export type InboundEvent = {
  content: string;
  meta: ChannelInboundMeta;
};

export type ChannelInboundMeta = Record<string, string | undefined>;
```

`meta` 는 채널마다 다름:
- Telegram — `chat_id`, `message_id`, `user_id`, `attachment_file_id`, ...
- CLI — `connection_id` (현재 cb-menu 는 RPC 라 inbound 안 씀)

dispatcher 는 `meta` 내부를 들여다보지 않는다. 채널-특화 처리는 채널 안에서.

직렬화 (IpcInbound) 와 호환되도록 모든 값은 `string` (또는 undefined).

### PermissionReply

```typescript
export type PermissionBehavior = "allow" | "deny";
export type PermissionReplyHandler = (
  requestId: string,
  behavior: PermissionBehavior,
) => void;
```

dispatcher 가 `requestPermission(req)` 으로 권한 UI 를 띄우면, 사용자가 응답한 결과는 `ChannelDeps.onPermissionReply` 로 보낸다.

---

## ChannelDeps — 생성자 주입

```typescript
export type ChannelDeps = {
  onInbound: InboundHandler;
  onPermissionReply: PermissionReplyHandler;
};
```

dispatcher 가 채널을 만들 때 함께 넘기는 콜백 묶음. 모든 채널이 받는다.

채널-특화 의존성은 ChannelDeps 를 확장:

```typescript
// TelegramChannel
export type TelegramChannelDeps = ChannelDeps & {
  tg: TelegramClient;
  config: Config;
  registry: Registry;
  sessions: SessionManager;
  onActiveChangedRequest: (previousId: string | undefined) => void;
};

// TmuxStatusChannel
export type TmuxStatusChannelDeps = ChannelDeps & {
  registry: Registry;
};
```

> **생성자 주입을 강제하는 이유** — setter 패턴은 "부분 구성" 상태를 허용하고 누락 시 컴파일러가 못 잡는다. 생성자 시점에 모두 받아야 `start()` 호출 직후 곧바로 사용 가능한 상태가 보장됨.

---

## fan-out 패턴

`dispatcher.ts` 의 핵심 한 줄짜리 패턴:

```typescript
let channels: Channel[] = [];

function announce(text: string): void {
  for (const ch of channels) ch.announce(text);
}

function notifyAll(evt: SessionEvent): void {
  for (const ch of channels) ch.notify(evt);
}
```

채널 인스턴스화 (`dispatcher.ts:276-294`):

```typescript
const telegramChannel = tg
  ? new TelegramChannel({
      tg, config, registry, sessions,
      onInbound, onPermissionReply, onActiveChangedRequest,
    })
  : undefined;

const tmuxChannel = new TmuxStatusChannel({
  registry, onInbound: noop, onPermissionReply: noop,
});

channels = telegramChannel ? [telegramChannel, tmuxChannel] : [tmuxChannel];
```

CLI-only 모드면 `tg` 가 없어서 `telegramChannel` 이 undefined → tmux 채널 하나만 동작. Telegram 모드면 둘 다.

이벤트 발생 지점에서:

```typescript
const sid = await sessions.spawn(...);
notifyAll({
  type: "spawned",
  sessionId: sid,
  label,
  resumed: false,
  autoSwitched: true,
});
```

dispatcher 는 어떤 채널이 활성화되어 있는지 모르고, 모르는 채로 같은 이벤트를 모두에게 보낸다.

---

## 에러 계약

> **`notify` / `announce` / `requestPermission` 은 throw 하지 않는다.**

- 채널 내부에서 실패하면 `anomaly.log()` 로 기록하고 best-effort 동작
- 한 채널의 실패가 dispatcher 흐름이나 다른 채널을 막아선 안 됨
- 예: Telegram API rate limit → TelegramChannel 안에서 흡수 + anomaly 기록 + retry. dispatcher 는 모름. tmux minimap 은 정상 갱신

이 계약 덕분에 dispatcher 의 fan-out loop 가 try/catch 없이 단순함:

```typescript
function notifyAll(evt: SessionEvent): void {
  for (const ch of channels) ch.notify(evt);  // throw 안 함이 보장됨
}
```

---

## 슬래시 명령 처리

> **slash 명령은 채널 내부에서 완결된다.**

picker UI, 액션 실행 모두 채널이 직접:

- TelegramChannel: SlashHandler 가 InlineKeyboard picker → 콜백에서 `sessions.spawn` 등 호출
- cb-menu (Channel 외부의 RPC 모델): 자체 키보드 입력 → `cli_request` RPC → dispatcher

dispatcher 는 결과 SessionEvent (`spawned`, `killed` 등) 만 받는다. slash 파싱 로직은 채널 외부에 있을 필요 없음.

이는 채널마다 매체별 picker UX 가 다르기 때문 (Telegram InlineKeyboard vs Ink ListSelect vs Slack BlockKit). 공통화하면 어느 한쪽이 어색해진다.

---

## 신규 채널 추가하는 법

`src/channels/<name>/` 디렉토리를 만들고:

1. **Channel 구현체** — `class FooChannel implements Channel`
2. **ChannelDeps 확장** — 매체별 의존성 정의
3. **dispatcher.ts 의 channels 배열에 인스턴스 추가**

예 — Slack 채널 골격:

```typescript
// src/channels/slack/channel.ts
export class SlackChannel implements Channel {
  readonly name = "slack";

  constructor(private readonly deps: SlackChannelDeps) {}

  async start(): Promise<void> {
    await this.deps.client.connect();
    this.deps.client.onMessage((msg) => {
      this.deps.onInbound({
        content: msg.text,
        meta: { channel: msg.channel, thread_ts: msg.thread_ts },
      });
    });
  }

  async stop(): Promise<void> { /* ... */ }

  notify(evt: SessionEvent): void {
    switch (evt.type) {
      case "spawned": this.postMessage(`🟢 ${evt.label} 시작됨`); break;
      case "reply_sent": /* typing 해제 */ break;
      // ...
    }
  }

  announce(text: string): void { this.postMessage(text); }

  requestPermission(req: PermissionRequest): void {
    this.postBlockKit(req); // Slack 의 inline button UI
  }
}
```

`src/core/*` 와 다른 채널은 변경 없음.

---

## Phase 도입 이력

원본 channel.ts 헤더 코멘트:

> Phase 0 — 인터페이스 정의만. 기존 코드는 이 파일을 import 하지 않으므로 컴파일 영향 없음. Phase 1 에서 dispatcher 와 TelegramChannel 이 이 인터페이스에 맞춰 리팩터링된다.

현재 (Phase 4):
- TelegramChannel — 구현됨
- TmuxStatusChannel — 구현됨
- cb-menu — Channel 인터페이스를 직접 구현하지는 않고 RPC 기반 (SessionEvent 를 polling 으로 가져옴). 향후 push 모델 전환 가능

---

## Known Fragility

- 채널이 늘어나면 dispatcher 의 channels 배열도 길어짐 — registry/wire-up 코드를 자동화하면 깔끔해질 수 있으나 현재 수가 적어 수동 wiring 충분
- `ChannelInboundMeta` 가 `Record<string, string | undefined>` 라 타입 안전성이 약함. 채널별 meta 타입을 union 으로 좁히는 작업이 향후 가능

---

## 참고

- 인터페이스 정의: `src/core/channel.ts`
- TelegramChannel 구현: `src/channels/telegram/channel.ts`
- TmuxStatusChannel 구현: `src/channels/tmux/channel.ts`
- cb-menu (RPC 기반): [07-cli-channel.md](07-cli-channel.md)
