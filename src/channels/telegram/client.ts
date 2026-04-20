import { Bot, InputFile, type InlineKeyboard } from "grammy";
import { extname } from "node:path";
import * as anomaly from "../anomaly.ts";

const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp"]);

export type Format = "text" | "markdownv2";

type ReplyOpts = {
  replyTo?: number;
  format?: Format;
};

export class TelegramClient {
  readonly bot: Bot;

  constructor(public readonly token: string) {
    this.bot = new Bot(token);
  }

  private parseMode(format?: Format): "MarkdownV2" | undefined {
    return format === "markdownv2" ? "MarkdownV2" : undefined;
  }

  async sendMessage(
    chatId: string,
    text: string,
    opts: ReplyOpts = {},
  ): Promise<number> {
    try {
      const pm = this.parseMode(opts.format);
      const msg = await this.bot.api.sendMessage(chatId, text, {
        ...(opts.replyTo ? { reply_parameters: { message_id: opts.replyTo } } : {}),
        ...(pm ? { parse_mode: pm } : {}),
      });
      return msg.message_id;
    } catch (err) {
      anomaly.log("telegram_api_failed", {
        op: "sendMessage",
        chatId,
        error: String(err),
      });
      throw err;
    }
  }

  async sendFile(
    chatId: string,
    filePath: string,
    replyTo?: number,
  ): Promise<number> {
    try {
      const ext = extname(filePath).toLowerCase();
      const input = new InputFile(filePath);
      const opts = replyTo ? { reply_parameters: { message_id: replyTo } } : {};
      if (IMAGE_EXTS.has(ext)) {
        const sent = await this.bot.api.sendPhoto(chatId, input, opts);
        return sent.message_id;
      }
      const sent = await this.bot.api.sendDocument(chatId, input, opts);
      return sent.message_id;
    } catch (err) {
      anomaly.log("telegram_api_failed", {
        op: "sendFile",
        chatId,
        filePath,
        error: String(err),
      });
      throw err;
    }
  }

  async editMessage(
    chatId: string,
    messageId: number,
    text: string,
    format?: Format,
  ): Promise<void> {
    try {
      const pm = this.parseMode(format);
      await this.bot.api.editMessageText(chatId, messageId, text, {
        ...(pm ? { parse_mode: pm } : {}),
      });
    } catch (err) {
      anomaly.log("telegram_api_failed", {
        op: "editMessageText",
        chatId,
        messageId,
        error: String(err),
      });
      throw err;
    }
  }

  async setReaction(chatId: string, messageId: number, emoji: string): Promise<void> {
    try {
      await this.bot.api.setMessageReaction(chatId, messageId, [
        { type: "emoji", emoji: emoji as never },
      ]);
    } catch (err) {
      anomaly.log("telegram_api_failed", {
        op: "setMessageReaction",
        chatId,
        messageId,
        emoji,
        error: String(err),
      });
      throw err;
    }
  }

  async sendWithKeyboard(
    chatId: string,
    text: string,
    keyboard: InlineKeyboard,
  ): Promise<number> {
    try {
      const sent = await this.bot.api.sendMessage(chatId, text, {
        reply_markup: keyboard,
      });
      return sent.message_id;
    } catch (err) {
      anomaly.log("telegram_api_failed", {
        op: "sendWithKeyboard",
        chatId,
        error: String(err),
      });
      throw err;
    }
  }

  async editWithKeyboard(
    chatId: string,
    messageId: number,
    text: string,
    keyboard?: InlineKeyboard,
  ): Promise<void> {
    try {
      await this.bot.api.editMessageText(chatId, messageId, text, {
        ...(keyboard ? { reply_markup: keyboard } : {}),
      });
    } catch (err) {
      anomaly.log("telegram_api_failed", {
        op: "editWithKeyboard",
        chatId,
        messageId,
        error: String(err),
      });
    }
  }

  async setCommands(
    commands: Array<{ command: string; description: string }>,
  ): Promise<void> {
    try {
      await this.bot.api.setMyCommands(commands);
    } catch (err) {
      anomaly.log("telegram_api_failed", {
        op: "setMyCommands",
        error: String(err),
      });
    }
  }

  async getFilePath(fileId: string): Promise<string> {
    try {
      const file = await this.bot.api.getFile(fileId);
      if (!file.file_path) {
        throw new Error("file.file_path missing from getFile response");
      }
      return file.file_path;
    } catch (err) {
      anomaly.log("telegram_api_failed", {
        op: "getFile",
        fileId,
        error: String(err),
      });
      throw err;
    }
  }
}
