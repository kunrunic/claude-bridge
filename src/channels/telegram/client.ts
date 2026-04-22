import { Bot, InputFile, type InlineKeyboard } from "grammy";
import { extname } from "node:path";
import * as anomaly from "../../core/anomaly.ts";

const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp"]);

export type Format = "text" | "markdownv2";

type ReplyOpts = {
  replyTo?: number;
  format?: Format;
};

// Per-call timeout + retry policy for every Telegram Bot API invocation.
// Node's built-in fetch has no default response timeout, so a stalled path
// would otherwise hang indefinitely. Retry handles transient network loss.
//
// Duplicate risk note: for sendMessage/sendFile the TCP hang case (what we
// retry) almost never reaches Telegram's side, so duplicate sends are rare.
// Silent message loss is a worse failure mode than occasional duplicates.
const DEFAULT_API_TIMEOUT_MS = 15_000;
const DEFAULT_RETRY_DELAYS_MS = [0, 2_000, 5_000]; // 3 attempts total

// Telegram 400 responses we KNOW won't improve by retrying. Some are
// success-equivalents (the intent is already met); callers can map these
// to success at the method layer.
const NON_RETRIABLE_SUBSTRINGS = [
  "message is not modified", // edit: desired content already shown
  "message to unpin not found", // unpin: pin already gone
  "message to pin not found", // pin: target deleted
  "message not found", // react/edit: target deleted
  "chat not found", // bad chatId
  "bot was blocked by the user",
  "user is deactivated",
  "not enough rights",
];

function isNonRetriable(err: unknown): boolean {
  const msg = String(err).toLowerCase();
  return NON_RETRIABLE_SUBSTRINGS.some((p) => msg.includes(p));
}

export class TelegramClient {
  readonly bot: Bot;

  constructor(
    public readonly token: string,
    private readonly apiTimeoutMs: number = DEFAULT_API_TIMEOUT_MS,
    private readonly retryDelaysMs: readonly number[] = DEFAULT_RETRY_DELAYS_MS,
  ) {
    this.bot = new Bot(token);
  }

  private parseMode(format?: Format): "MarkdownV2" | undefined {
    return format === "markdownv2" ? "MarkdownV2" : undefined;
  }

  private timeoutRace<T>(op: string, p: Promise<T>): Promise<T> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(
        () => reject(new Error(`${op} timeout after ${this.apiTimeoutMs}ms`)),
        this.apiTimeoutMs,
      );
    });
    return Promise.race([p, timeout]).finally(() => {
      if (timer) clearTimeout(timer);
    });
  }

  // Wrap an API call with timeout + retry. Each attempt is logged with
  // caller-supplied context; on final failure the last error is rethrown.
  //
  // IMPORTANT: `make` must be a *factory* (deferred call) — we invoke it
  // fresh per attempt so a retry actually issues a new HTTP request rather
  // than awaiting the same already-failed Promise.
  //
  // Some errors are permanent (bad input, already-in-goal-state) — retrying
  // is wasteful. `isNonRetriable(err)` short-circuits: throw immediately
  // without retry or per-attempt log noise. Callers then decide whether to
  // map the error to success (e.g. "message is not modified" for edit).
  private async callApi<T>(
    op: string,
    ctx: Record<string, unknown>,
    make: () => Promise<T>,
  ): Promise<T> {
    const delays = this.retryDelaysMs;
    let lastErr: unknown;
    for (let i = 0; i < delays.length; i++) {
      if (delays[i]! > 0) await new Promise((r) => setTimeout(r, delays[i]));
      try {
        return await this.timeoutRace(op, make());
      } catch (err) {
        lastErr = err;
        if (isNonRetriable(err)) {
          anomaly.log("telegram_api_failed", {
            op,
            ...ctx,
            attempt: i + 1,
            nonRetriable: true,
            error: String(err),
          });
          throw err;
        }
        anomaly.log("telegram_api_failed", {
          op,
          ...ctx,
          attempt: i + 1,
          error: String(err),
        });
      }
    }
    throw lastErr;
  }

  async sendMessage(
    chatId: string,
    text: string,
    opts: ReplyOpts = {},
  ): Promise<number> {
    const pm = this.parseMode(opts.format);
    const msg = await this.callApi("sendMessage", { chatId }, () =>
      this.bot.api.sendMessage(chatId, text, {
        ...(opts.replyTo ? { reply_parameters: { message_id: opts.replyTo } } : {}),
        ...(pm ? { parse_mode: pm } : {}),
      }),
    );
    return msg.message_id;
  }

  async sendFile(
    chatId: string,
    filePath: string,
    replyTo?: number,
  ): Promise<number> {
    const ext = extname(filePath).toLowerCase();
    const input = new InputFile(filePath);
    const opts = replyTo ? { reply_parameters: { message_id: replyTo } } : {};
    if (IMAGE_EXTS.has(ext)) {
      const sent = await this.callApi("sendPhoto", { chatId, filePath }, () =>
        this.bot.api.sendPhoto(chatId, input, opts),
      );
      return sent.message_id;
    }
    const sent = await this.callApi("sendDocument", { chatId, filePath }, () =>
      this.bot.api.sendDocument(chatId, input, opts),
    );
    return sent.message_id;
  }

  async editMessage(
    chatId: string,
    messageId: number,
    text: string,
    format?: Format,
  ): Promise<void> {
    const pm = this.parseMode(format);
    try {
      await this.callApi("editMessageText", { chatId, messageId }, () =>
        this.bot.api.editMessageText(chatId, messageId, text, {
          ...(pm ? { parse_mode: pm } : {}),
        }),
      );
    } catch (err) {
      // "message is not modified" means the desired content already equals
      // current content — goal met, treat as success.
      if (String(err).toLowerCase().includes("message is not modified")) return;
      throw err;
    }
  }

  async setReaction(chatId: string, messageId: number, emoji: string): Promise<void> {
    await this.callApi("setMessageReaction", { chatId, messageId, emoji }, () =>
      this.bot.api.setMessageReaction(chatId, messageId, [
        { type: "emoji", emoji: emoji as never },
      ]),
    );
  }

  async sendWithKeyboard(
    chatId: string,
    text: string,
    keyboard: InlineKeyboard,
  ): Promise<number> {
    const sent = await this.callApi("sendWithKeyboard", { chatId }, () =>
      this.bot.api.sendMessage(chatId, text, { reply_markup: keyboard }),
    );
    return sent.message_id;
  }

  async editWithKeyboard(
    chatId: string,
    messageId: number,
    text: string,
    keyboard?: InlineKeyboard,
  ): Promise<void> {
    // Note: swallows on failure (callers use edits as best-effort UI updates,
    // e.g. "cancelled" message replacement). callApi already logs each attempt.
    try {
      await this.callApi("editWithKeyboard", { chatId, messageId }, () =>
        this.bot.api.editMessageText(chatId, messageId, text, {
          ...(keyboard ? { reply_markup: keyboard } : {}),
        }),
      );
    } catch {
      // intentionally swallowed
    }
  }

  async setCommands(
    commands: Array<{ command: string; description: string }>,
  ): Promise<void> {
    // Best-effort; not worth throwing. callApi logs each attempt.
    try {
      await this.callApi("setMyCommands", {}, () =>
        this.bot.api.setMyCommands(commands),
      );
    } catch {
      // intentionally swallowed
    }
  }

  async pinMessage(
    chatId: string,
    messageId: number,
    disableNotification = true,
  ): Promise<void> {
    await this.callApi("pinChatMessage", { chatId, messageId }, () =>
      this.bot.api.pinChatMessage(chatId, messageId, {
        disable_notification: disableNotification,
      }),
    );
  }

  async unpinMessage(chatId: string, messageId: number): Promise<void> {
    // Propagate so callers can decide (retry / ignore). "message to unpin
    // not found" is effectively success and callers can match on it.
    await this.callApi("unpinChatMessage", { chatId, messageId }, () =>
      this.bot.api.unpinChatMessage(chatId, messageId),
    );
  }

  async getFilePath(fileId: string): Promise<string> {
    const file = await this.callApi("getFile", { fileId }, () =>
      this.bot.api.getFile(fileId),
    );
    if (!file.file_path) {
      throw new Error("file.file_path missing from getFile response");
    }
    return file.file_path;
  }
}
