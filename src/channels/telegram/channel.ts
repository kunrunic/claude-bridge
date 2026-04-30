/**
 * TelegramChannel — Channel 인터페이스의 Telegram 구현체.
 *
 * dispatcher.ts 에 흩어져 있던 Telegram 전용 로직을 한 곳에 모은다:
 *  - Poller (사용자 입력 수신)
 *  - SlashHandler (슬래시 명령 picker UI / 액션)
 *  - Pin operations queue (active session 표시 / inactive reply 핀)
 *  - Animation (busy emoji rotation)
 *  - 권한 요청 UI (compact / expanded keyboard)
 *  - announce / SessionEvent 텍스트 합성
 *
 * dispatcher 는 이 채널의 내부 구현(pin, animation 등)을 알지 못한다.
 * SessionEvent / announce / requestPermission 호출만으로 통신한다.
 */

import type { Channel, ChannelDeps, InboundEvent, PermissionRequest, SessionEvent } from "../../core/channel.ts";
import type { Registry } from "../../core/registry.ts";
import type { SessionManager } from "../../core/SessionManager.ts";
import type { TelegramClient } from "./client.ts";
import type { Config } from "./config.ts";
import { Poller } from "./poller.ts";
import { SlashHandler } from "./SlashHandler.ts";
import { gate } from "./access.ts";
import {
  buildCompactKeyboard,
  formatCompactPrompt,
  pendingPermissions,
} from "./permissions.ts";
import { updateActivePin } from "../../core/pin.ts";
import * as slash from "../../core/slash.ts";
import * as anomaly from "../../core/anomaly.ts";
import { acquirePollingLock, releasePollingLock } from "../../core/lifecycle.ts";

const BUSY_FRAMES = ["🤔", "💭", "🧐", "🤓", "💡", "🤯"] as const;
const ANIM_TICK_MS = 5_000;

export type TelegramChannelDeps = ChannelDeps & {
  tg: TelegramClient;
  config: Config;
  registry: Registry;
  sessions: SessionManager;
  /**
   * 채널 내부에서 active session 이 바뀐 직후 호출.
   * dispatcher 가 active_changed / active_cleared 이벤트를 모든 채널에
   * fan-out 할 책임을 가진다.
   */
  onActiveChangedRequest: (previousId: string | undefined) => void;
};

export class TelegramChannel implements Channel {
  readonly name = "telegram";

  private readonly poller: Poller;
  private readonly slashHandler: SlashHandler;

  // Pin 작업 직렬화 — concurrent 호출 시 unpin/pin 시퀀스가 race 하면 orphan
  // pin 또는 잘못된 순서가 남는다.
  private pinOpsQueue: Promise<void> = Promise.resolve();

  // Telegram 메시지 ID 들 — inactive 세션의 reply 가 핀된 메시지.
  // 사용자가 그 세션을 떠나거나 세션이 종료되면 unpin.
  private readonly pinnedReplyIds = new Map<string, number[]>();

  // Animation state — busy emoji 가 5초마다 회전.
  private animMsgId: number | undefined;
  private animFrame = 0;
  private animTimer: ReturnType<typeof setInterval> | undefined;

  constructor(private readonly deps: TelegramChannelDeps) {
    this.poller = new Poller(
      this.deps.tg,
      this.deps.config,
      (evt) => this.onPollerInbound(evt),
      this.deps.onPermissionReply,
      (action, target, chatId, msgId) =>
        this.slashHandler.onSessionAction(action, target, chatId, msgId),
    );
    this.slashHandler = new SlashHandler({
      registry: this.deps.registry,
      tg: this.deps.tg,
      sessions: this.deps.sessions,
      onActiveChanged: (leftId) => this.deps.onActiveChangedRequest(leftId),
    });
  }

  // ── Channel: lifecycle ────────────────────────────────────────────────────

  // CB_POLL_DISABLED=1 환경에서는 polling 자체를 건너뛴다 (테스트 / handoff
  // 시나리오용). 이 경우 lock 도 잡지 않으므로 다른 인스턴스와 충돌하지 않음.
  private pollEnabled = process.env.CB_POLL_DISABLED !== "1";

  async start(): Promise<void> {
    if (!this.pollEnabled) {
      anomaly.log("server_startup", {
        where: "TelegramChannel.start",
        note: "polling disabled (CB_POLL_DISABLED=1)",
      });
      return;
    }
    // 같은 bot token 으로 polling 중복 방지 — 이전 인스턴스 발견되면 SIGTERM 후 PID file 갱신.
    await acquirePollingLock("TelegramChannel.start");
    await this.deps.tg.setCommands(slash.BOT_COMMANDS);
    await this.poller.start();
  }

  async stop(): Promise<void> {
    this.stopAnimation();
    void this.poller.stop();
    await this.cleanupStalePins();
    if (this.pollEnabled) releasePollingLock();
  }

  // ── Channel: outbound ─────────────────────────────────────────────────────

  notify(evt: SessionEvent): void {
    switch (evt.type) {
      case "spawned": {
        const word = evt.resumed ? "이어하기 준비됨" : "준비됨";
        this.announce(`✅ [${evt.sessionId}][${evt.label}] ${word} — 자동 전환됨`);
        break;
      }
      case "killed":
        // dispatcher 측 SessionManager 가 관리. Telegram 은 별도 announce 안 함
        // (slash/액션의 즉각 응답으로 이미 사용자에게 알려짐).
        break;
      case "active_changed":
        if (evt.previousId) void this.unpinRepliesOf(evt.previousId);
        void this.switchActivePin(evt.sessionId, evt.label);
        break;
      case "active_cleared":
        if (evt.previousId) void this.unpinRepliesOf(evt.previousId);
        void this.doUpdateActivePin();
        break;
      case "state_changed":
        // Telegram 은 signal 변화를 실시간으로 표시하지 않는다 (rate limit / UX 부담).
        // tmux/CLI 채널이 status bar 갱신용으로 사용.
        break;
      case "inbound_delivered":
        this.onInboundDelivered(evt.sessionId, evt.label, evt.meta);
        break;
      case "reply_sent":
        this.onReplySent(evt.sessionId, evt.messageIds);
        break;
      case "disconnected": {
        void this.unpinRepliesOf(evt.sessionId);
        this.announce(
          `⚠️ [${evt.sessionId}][${evt.label}] 연결 끊김 — 자동 재연결 시도 중\n` +
            `재연결 실패 시 /new 또는 /resume 으로 새 세션을 시작하세요.`,
        );
        void this.doUpdateActivePin();
        break;
      }
      case "reconnected":
        this.announce(`🔄 [${evt.sessionId}][${evt.label}] 재연결됨`);
        break;
    }
  }

  announce(text: string): void {
    const preview = text.slice(0, 80);
    if (!this.deps.config.defaultChatId) {
      anomaly.log("dispatcher_announce_skip", { reason: "no_default_chat", preview });
      return;
    }
    anomaly.log("dispatcher_announce_start", { preview });
    void this.deps.tg.sendMessage(this.deps.config.defaultChatId, text).then(
      () => {
        anomaly.log("dispatcher_announce_ok", { preview });
      },
      (err) => {
        anomaly.log("dispatcher_announce_give_up", {
          preview,
          error: String(err),
        });
      },
    );
  }

  requestPermission(req: PermissionRequest): void {
    pendingPermissions.set(req.requestId, {
      tool_name: req.toolName,
      description: req.description,
      input_preview: req.inputPreview,
    });
    const session = this.deps.registry.get(req.sessionId);
    const label = session ? `[${session.id}][${session.label}] ` : "";
    const keyboard = buildCompactKeyboard(req.requestId);
    const prompt = `${label}${formatCompactPrompt(req.toolName)}`;
    for (const chatId of this.deps.config.allowlist) {
      void this.deps.tg.sendWithKeyboard(chatId, prompt, keyboard).catch((err) => {
        anomaly.log("channel_reply_failed", {
          op: "permission_request_relay",
          chatId,
          requestId: req.requestId,
          error: String(err),
        });
      });
    }
  }

  // ── Inbound ───────────────────────────────────────────────────────────────

  /**
   * Poller 가 받은 사용자 메시지 처리 분기.
   * - slash 명령은 채널 내부에서 SlashHandler 로 직접 처리.
   * - 일반 메시지는 dispatcher.onInbound 콜백으로 전달.
   */
  private async onPollerInbound(evt: InboundEvent): Promise<void> {
    const slashCmd = slash.parse(evt.content);
    if (slashCmd) {
      const chatId = evt.meta.chat_id;
      if (chatId) await this.slashHandler.handle(slashCmd, chatId);
      return;
    }
    await this.deps.onInbound(evt);
  }

  /**
   * dispatcher 가 active session 에 inbound 를 전달한 직후 호출.
   * Telegram 측 ack: 사용자에게 "사용중" 표시 + animation 시작 + reaction.
   */
  private onInboundDelivered(
    sessionId: string,
    label: string,
    meta: InboundEvent["meta"] | undefined,
  ): void {
    const chatId = meta?.chat_id;
    const messageId = meta?.message_id;
    this.startAnimation();
    if (chatId) {
      void this.deps.tg
        .sendMessage(chatId, `[${sessionId}][${label}] 사용중`)
        .catch(() => {});
      if (messageId) {
        void this.deps.tg.setReaction(chatId, Number(messageId), "✍").catch(() => {});
      }
    }
  }

  // ── Pin operations ────────────────────────────────────────────────────────

  /** Pin 작업 직렬화. concurrent unpin/pin race 방지. */
  private enqueuePinOp<T>(op: () => Promise<T>, where: string): Promise<T> {
    const p = this.pinOpsQueue.then(op);
    this.pinOpsQueue = p.then(
      () => {},
      (err) => {
        anomaly.log("anomaly_self_error", { where, error: String(err) });
      },
    );
    return p;
  }

  private syncPinnedRepliesToRegistry(): void {
    this.deps.registry.setPinnedReplies(Object.fromEntries(this.pinnedReplyIds));
  }

  private doUpdateActivePin(): Promise<void> {
    const chatId = this.deps.config.defaultChatId;
    if (!chatId) return Promise.resolve();
    return this.enqueuePinOp(
      () => updateActivePin(this.deps.registry, this.deps.tg, chatId),
      "doUpdateActivePin",
    );
  }

  pinInactiveReplies(sessionId: string, messageIds: number[]): Promise<void> {
    if (!this.deps.config.defaultChatId) return Promise.resolve();
    const chat = this.deps.config.defaultChatId;
    return this.enqueuePinOp(async () => {
      const existing = this.pinnedReplyIds.get(sessionId) ?? [];
      for (const mid of messageIds) {
        try {
          await this.deps.tg.pinMessage(chat, mid, true);
          existing.push(mid);
        } catch (err) {
          anomaly.log("telegram_api_failed", {
            op: "pinInactiveReply",
            sessionId,
            messageId: mid,
            error: String(err),
          });
        }
      }
      this.pinnedReplyIds.set(sessionId, existing);
      this.syncPinnedRepliesToRegistry();
    }, "pinInactiveReplies");
  }

  private unpinRepliesOf(sessionId: string): Promise<void> {
    if (!this.deps.config.defaultChatId) return Promise.resolve();
    const chat = this.deps.config.defaultChatId;
    const ids = this.pinnedReplyIds.get(sessionId);
    if (!ids || ids.length === 0) return Promise.resolve();
    this.pinnedReplyIds.delete(sessionId);
    this.syncPinnedRepliesToRegistry();
    return this.enqueuePinOp(async () => {
      for (const mid of ids) {
        try {
          await this.deps.tg.unpinMessage(chat, mid);
        } catch (err) {
          anomaly.log("telegram_api_failed", {
            op: "unpinReplyOnLeave",
            sessionId,
            messageId: mid,
            error: String(err),
          });
        }
      }
    }, "unpinRepliesOf");
  }

  // 세션 전환 시: 새 "⚡ [id][label] 활성화" 메시지를 보내고 핀 → 이전 핀 해제.
  // 이렇게 하면 핀이 현재 채팅 위치에 생기므로, 탭 시 옛 위치가 아닌 전환
  // 시점으로 스크롤된다.
  private switchActivePin(id: string, label: string): Promise<void> {
    const chatId = this.deps.config.defaultChatId;
    if (!chatId) return Promise.resolve();
    return this.enqueuePinOp(async () => {
      const currentPin = this.deps.registry.getActivePin();
      let newMsgId: number;
      try {
        newMsgId = await this.deps.tg.sendMessage(chatId, `⚡ [${id}][${label}] 활성화`);
      } catch (err) {
        anomaly.log("telegram_api_failed", { op: "switchActivePin.send", error: String(err) });
        return;
      }
      try {
        await this.deps.tg.pinMessage(chatId, newMsgId, true);
        this.deps.registry.setActivePin({ chatId, messageId: newMsgId });
      } catch (err) {
        anomaly.log("telegram_api_failed", { op: "switchActivePin.pin", error: String(err) });
      }
      if (currentPin) {
        await this.deps.tg.unpinMessage(currentPin.chatId, currentPin.messageId).catch(() => {});
      }
    }, "switchActivePin");
  }

  // 새 reply 메시지를 핀한 직후, Telegram 은 가장 최근 핀을 banner 상단에 표시.
  // active session 요약을 다시 위로 올리려면 unpin 후 다시 pin (같은 메시지).
  private bumpActivePinToTop(): Promise<void> {
    return this.enqueuePinOp(async () => {
      const pin = this.deps.registry.getActivePin();
      if (!pin) return;
      const { chatId, messageId } = pin;
      try {
        await this.deps.tg.unpinMessage(chatId, messageId);
      } catch {
        // 이미 사라진 경우 — 무시하고 재핀 시도
      }
      try {
        await this.deps.tg.pinMessage(chatId, messageId, true);
      } catch (err) {
        anomaly.log("telegram_api_failed", {
          op: "bumpActivePinToTop",
          error: String(err),
        });
      }
    }, "bumpActivePinToTop");
  }

  // ── Animation ─────────────────────────────────────────────────────────────

  private startAnimation(): void {
    if (!this.deps.config.defaultChatId) return;
    if (this.animMsgId !== undefined || this.animTimer !== undefined) return;
    this.animFrame = 0;
    const firstEmoji = BUSY_FRAMES[0]!;
    void this.deps.tg
      .sendMessage(this.deps.config.defaultChatId, firstEmoji)
      .then((id) => {
        this.animMsgId = id;
      })
      .catch(() => {});
    this.animTimer = setInterval(() => {
      if (!this.deps.config.defaultChatId || this.animMsgId === undefined) return;
      this.animFrame++;
      const emoji = BUSY_FRAMES[this.animFrame % BUSY_FRAMES.length]!;
      void this.deps.tg.bot.api
        .editMessageText(this.deps.config.defaultChatId, this.animMsgId, emoji)
        .catch(() => {});
    }, ANIM_TICK_MS);
  }

  private stopAnimation(): void {
    if (this.animTimer !== undefined) {
      clearInterval(this.animTimer);
      this.animTimer = undefined;
    }
  }

  /**
   * Claude 가 reply 도구를 호출 → animation 종료 + (필요 시) 비활성 세션 reply 핀.
   * dispatcher 에서 "이 세션이 active 인가" 정보가 SessionEvent.reply_sent 에는
   * 들어 있지 않다. registry 를 직접 조회.
   */
  private onReplySent(sessionId: string, messageIds?: number[]): void {
    const active = this.deps.registry.active();
    if (active?.id !== sessionId && messageIds && messageIds.length > 0) {
      void this.pinInactiveReplies(sessionId, messageIds);
      void this.doUpdateActivePin();
      void this.bumpActivePinToTop();
    }
    this.stopAnimation();
    if (!this.deps.config.defaultChatId || this.animMsgId === undefined) return;
    const msgId = this.animMsgId;
    this.animMsgId = undefined;
    this.animFrame = 0;
    void this.deps.tg.bot.api
      .editMessageText(this.deps.config.defaultChatId, msgId, "✅")
      .then(() => new Promise<void>((r) => setTimeout(r, 5000)))
      .then(() => this.deps.tg.bot.api.deleteMessage(this.deps.config.defaultChatId!, msgId))
      .catch(() => {});
  }

  // ── Stale pin cleanup (start/stop 시) ────────────────────────────────────

  /**
   * 시작/종료 시 pin 잔여물 정리.
   *  - active pin: 이전 실행에서 남았는데 메시지가 사라졌을 수 있음.
   *  - reply pins: 동일.
   *  - "message to unpin not found" 는 이미 목적 달성 → registry 에서 제거.
   *  - 그 외 실패는 다음 실행에서 재시도하도록 registry 에 남김.
   */
  async cleanupStalePins(): Promise<void> {
    const tg = this.deps.tg;
    const registry = this.deps.registry;
    const wasGoalMet = async (msgId: number, chatId: string): Promise<boolean> => {
      try {
        await tg.unpinMessage(chatId, msgId);
        return true;
      } catch (err) {
        const m = String(err).toLowerCase();
        return m.includes("message to unpin not found") || m.includes("message not found");
      }
    };
    const stalePin = registry.getActivePin();
    if (stalePin && (await wasGoalMet(stalePin.messageId, stalePin.chatId))) {
      registry.setActivePin(undefined);
    }
    if (!this.deps.config.defaultChatId) return;
    const chat = this.deps.config.defaultChatId;
    const remaining: Record<string, number[]> = {};
    for (const [sid, ids] of this.pinnedReplyIds) {
      const stillPinned: number[] = [];
      for (const mid of ids) {
        if (!(await wasGoalMet(mid, chat))) stillPinned.push(mid);
      }
      if (stillPinned.length > 0) remaining[sid] = stillPinned;
    }
    this.pinnedReplyIds.clear();
    for (const [sid, ids] of Object.entries(remaining)) this.pinnedReplyIds.set(sid, ids);
    registry.setPinnedReplies(remaining);
  }

  /** 시작 직후 호출되어, 이전 실행에서 영속된 reply pins 을 메모리로 복원. */
  loadPersistedPinnedReplies(): void {
    const persisted = this.deps.registry.getPinnedReplies();
    for (const [sid, ids] of Object.entries(persisted)) {
      this.pinnedReplyIds.set(sid, ids);
    }
  }

  // ── Inbound ack helpers (used by dispatcher when access-gating) ──────────

  /**
   * dispatcher 가 inbound 를 reject 한 경우, "no active session" 같은 안내 텍스트를
   * 사용자에게 직접 보내야 할 때 사용. 일반 announce 와 달리 특정 chatId 로.
   */
  notifyChat(chatId: string, text: string): void {
    void this.deps.tg.sendMessage(chatId, text).catch(() => {});
  }

  /**
   * gate(allowlist) 검사 — 채널 내부 책임. dispatcher 는 호출하지 않음.
   * 단, dispatcher-handlers.handleInbound 가 announce 를 통해 사용자에게 알려야
   * 할 때 chatId 가 필요하므로 외부에 노출.
   */
  isAllowed(chatId: string, userId: string): boolean {
    return gate(this.deps.config, chatId, userId).action === "allow";
  }
}
