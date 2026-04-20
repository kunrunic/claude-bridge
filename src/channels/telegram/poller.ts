import type { TelegramClient } from "./client.ts";
import type { Config } from "../config.ts";
import { gate } from "../access.ts";
import { saveAttachment } from "../inbox.ts";
import {
  CALLBACK_RE,
  REPLY_RE,
  pendingPermissions,
  buildExpandedKeyboard,
  formatExpandedBody,
} from "../permissions.ts";
import * as anomaly from "../anomaly.ts";

export type InboundMeta = {
  chat_id: string;
  message_id?: string;
  user: string;
  user_id: string;
  ts: string;
  image_path?: string;
  attachment_kind?: string;
  attachment_file_id?: string;
  attachment_size?: string;
  attachment_mime?: string;
  attachment_name?: string;
};

export type InboundEvent = {
  content: string;
  meta: InboundMeta;
};

export type InboundHandler = (evt: InboundEvent) => void;
export type PermissionBehavior = "allow" | "deny";
export type PermissionReplyHandler = (
  requestId: string,
  behavior: PermissionBehavior,
) => void;

const SAFE_META_RE = /[<>\[\]\r\n;]/g;
const safe = (s: string | undefined): string | undefined =>
  s?.replace(SAFE_META_RE, "_");

export const CONFLICT_RE = /409:\s*Conflict/i;
const COLLISION_WINDOW_MS = 60_000;
const COLLISION_THRESHOLD = 3;
const COLLISION_ALERT_COOLDOWN_MS = 5 * 60_000;

export class Poller {
  private collisionTimestamps: number[] = [];
  private lastCollisionAlertTs = 0;

  constructor(
    private readonly tg: TelegramClient,
    private readonly config: Config,
    private readonly onInbound: InboundHandler,
    private readonly onPermissionReply: PermissionReplyHandler,
  ) {}

  private noteConflict(errText: string): void {
    if (!CONFLICT_RE.test(errText)) return;
    const now = Date.now();
    this.collisionTimestamps = this.collisionTimestamps.filter(
      (t) => now - t < COLLISION_WINDOW_MS,
    );
    this.collisionTimestamps.push(now);
    if (
      this.collisionTimestamps.length >= COLLISION_THRESHOLD &&
      now - this.lastCollisionAlertTs > COLLISION_ALERT_COOLDOWN_MS
    ) {
      this.lastCollisionAlertTs = now;
      anomaly.log("token_collision_detected", {
        count: this.collisionTimestamps.length,
        windowMs: COLLISION_WINDOW_MS,
      });
      const warn =
        "⚠️ token collision: another process is polling this bot token. " +
        "Inbound messages may be lost. Check for duplicate bot instances " +
        "(e.g. claude-plugins-official/telegram with the same TELEGRAM_BOT_TOKEN).";
      for (const chatId of this.config.allowlist) {
        void this.tg.sendMessage(chatId, warn).catch(() => {});
      }
    }
  }

  async start(): Promise<void> {
    const bot = this.tg.bot;

    bot.on("callback_query:data", async (ctx) => {
      await this.handleCallback(ctx);
    });

    bot.on("message:text", async (ctx) => {
      const text = ctx.message.text;
      const permMatch = REPLY_RE.exec(text);
      if (permMatch) {
        const first = permMatch[1]!.toLowerCase();
        const requestId = permMatch[2]!.toLowerCase();
        const behavior: PermissionBehavior = first.startsWith("y") ? "allow" : "deny";
        const senderId = ctx.from ? String(ctx.from.id) : "";
        if (!this.config.allowlist.includes(senderId)) {
          anomaly.log("inbound_no_active_session", {
            reason: "permission_reply_unauthorized",
            senderId,
          });
          return;
        }
        this.onPermissionReply(requestId, behavior);
        if (ctx.chat && ctx.message) {
          const emoji = behavior === "allow" ? "✅" : "❌";
          void this.tg
            .setReaction(String(ctx.chat.id), ctx.message.message_id, emoji)
            .catch(() => {});
        }
        return;
      }
      await this.handleInbound(ctx, text);
    });

    bot.on("message:photo", async (ctx) => {
      const caption = ctx.message.caption ?? "(image)";
      const photo = ctx.message.photo[ctx.message.photo.length - 1];
      if (!photo) return;
      const imagePath = await this.downloadSafely(photo.file_id);
      await this.handleInbound(ctx, caption, imagePath);
    });

    bot.on("message:document", async (ctx) => {
      const doc = ctx.message.document;
      const att: Partial<InboundMeta> = {
        attachment_kind: "document",
        attachment_file_id: doc.file_id,
      };
      if (doc.file_size != null) att.attachment_size = String(doc.file_size);
      if (doc.mime_type) att.attachment_mime = doc.mime_type;
      const name = safe(doc.file_name);
      if (name) att.attachment_name = name;
      await this.handleInbound(ctx, ctx.message.caption ?? "(document)", undefined, att);
    });

    bot.catch((err) => {
      anomaly.log("telegram_api_failed", {
        op: "poller.bot.catch",
        error: String(err.error),
      });
    });

    void (async () => {
      for (let attempt = 1; ; attempt++) {
        try {
          await bot.start({
            onStart: () => {
              attempt = 0;
            },
          });
          return;
        } catch (err) {
          const errText = String(err);
          anomaly.log("telegram_api_failed", {
            op: "poller.start",
            attempt,
            error: errText,
          });
          this.noteConflict(errText);
          const backoff = Math.min(30000, 1000 * 2 ** Math.min(attempt, 5));
          await new Promise((r) => setTimeout(r, backoff));
        }
      }
    })();
  }

  stop(): Promise<void> {
    return this.tg.bot.stop();
  }

  private async handleCallback(ctx: {
    callbackQuery: { data?: string; id: string; message?: { message_id: number; chat: { id: number | string }; text?: string } };
    from: { id: number };
    answerCallbackQuery: (opts?: { text?: string }) => Promise<unknown>;
  }): Promise<void> {
    const data = ctx.callbackQuery.data;
    if (!data) {
      await ctx.answerCallbackQuery().catch(() => {});
      return;
    }
    const m = CALLBACK_RE.exec(data);
    if (!m) {
      await ctx.answerCallbackQuery().catch(() => {});
      return;
    }
    const senderId = String(ctx.from.id);
    if (!this.config.allowlist.includes(senderId)) {
      await ctx.answerCallbackQuery({ text: "Not authorized." }).catch(() => {});
      return;
    }
    const behavior = m[1]!;
    const requestId = m[2]!;

    if (behavior === "more") {
      const details = pendingPermissions.get(requestId);
      const msg = ctx.callbackQuery.message;
      if (!details || !msg) {
        await ctx
          .answerCallbackQuery({ text: "Details no longer available." })
          .catch(() => {});
        return;
      }
      await this.tg.editWithKeyboard(
        String(msg.chat.id),
        msg.message_id,
        formatExpandedBody(details),
        buildExpandedKeyboard(requestId),
      );
      await ctx.answerCallbackQuery().catch(() => {});
      return;
    }

    this.onPermissionReply(requestId, behavior as PermissionBehavior);
    pendingPermissions.delete(requestId);
    const label = behavior === "allow" ? "✅ Allowed" : "❌ Denied";
    await ctx.answerCallbackQuery({ text: label }).catch(() => {});
    const msg = ctx.callbackQuery.message;
    if (msg && msg.text) {
      await this.tg.editWithKeyboard(
        String(msg.chat.id),
        msg.message_id,
        `${msg.text}\n\n${label}`,
      );
    }
  }

  private async downloadSafely(fileId: string): Promise<string | undefined> {
    try {
      const filePath = await this.tg.getFilePath(fileId);
      return await saveAttachment(this.tg.token, fileId, filePath);
    } catch {
      return undefined;
    }
  }

  private async handleInbound(
    ctx: { message?: { message_id?: number; date?: number }; from?: { id: number; username?: string }; chat?: { id: number | string } },
    text: string,
    imagePath?: string,
    attachment?: Partial<InboundMeta>,
  ): Promise<void> {
    const from = ctx.from;
    const chat = ctx.chat;
    if (!from || !chat) return;
    const chatId = String(chat.id);
    const userId = String(from.id);

    const decision = gate(this.config, chatId, userId);
    if (decision.action === "drop") {
      anomaly.log("inbound_no_active_session", {
        chatId,
        userId,
        reason: decision.reason,
        preview: text.slice(0, 40),
      });
      return;
    }

    const msgId = ctx.message?.message_id;
    const ts = ctx.message?.date ?? Math.floor(Date.now() / 1000);

    const meta: InboundMeta = {
      chat_id: chatId,
      ...(msgId != null ? { message_id: String(msgId) } : {}),
      user: from.username ?? String(from.id),
      user_id: userId,
      ts: new Date(ts * 1000).toISOString(),
      ...(imagePath ? { image_path: imagePath } : {}),
      ...(attachment ?? {}),
    };

    this.onInbound({ content: text, meta });
  }
}
