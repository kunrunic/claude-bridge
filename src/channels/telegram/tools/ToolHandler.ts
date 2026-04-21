import { statSync } from "node:fs";
import type { TelegramClient } from "../client.ts";
import type { Config } from "../config.ts";
import { ReplyArgs, ReactArgs, EditArgs, DownloadArgs } from "./definitions.ts";
import {
  chunkByLength,
  chunkByNewline,
  assertSendable,
  TG_TEXT_LIMIT,
  MAX_ATTACHMENT_BYTES,
} from "./text.ts";
import { assertAllowedChat } from "../access.ts";
import { saveAttachment } from "../inbox.ts";
import * as anomaly from "../../../core/anomaly.ts";

export type McpResult = {
  content: Array<{ type: "text"; text: string }>;
  isError?: boolean;
  /** Telegram message IDs sent by this tool call (reply only; for pin tracking). */
  message_ids?: number[];
};

/**
 * ToolHandler encapsulates the MCP CallToolRequestSchema handler logic.
 * Pure dispatch based on tool name; all I/O delegated to TelegramClient.
 */
export type SessionStateProvider = {
  readonly sessionActive: boolean;
  readonly sessionLabel: string;
};

export class ToolHandler {
  constructor(
    private readonly tg: TelegramClient,
    private readonly config: Config,
    private readonly sessionStateProvider?: SessionStateProvider,
  ) {}

  /**
   * Handle a tool call by name and arguments.
   * Validates input, checks access, executes tool, returns MCP result.
   */
  async handle(name: string, rawArgs: unknown): Promise<McpResult> {
    try {
      switch (name) {
        case "reply": {
          return await this.handleReply(rawArgs);
        }

        case "react": {
          return await this.handleReact(rawArgs);
        }

        case "edit_message": {
          return await this.handleEditMessage(rawArgs);
        }

        case "download_attachment": {
          return await this.handleDownloadAttachment(rawArgs);
        }

        default: {
          anomaly.log("mcp_unknown_method", { method: name });
          return {
            isError: true,
            content: [{ type: "text", text: `unknown tool: ${name}` }],
          };
        }
      }
    } catch (err) {
      anomaly.log("channel_reply_failed", {
        tool: name,
        error: String(err),
      });
      return {
        isError: true,
        content: [{ type: "text", text: `${name} failed: ${String(err)}` }],
      };
    }
  }

  private async handleReply(rawArgs: unknown): Promise<McpResult> {
    const args = ReplyArgs.parse(rawArgs);
    assertAllowedChat(this.config, args.chat_id);

    // If this session is inactive, prefix text with session label on its own
    // line so the user can quickly tell which session the reply is from.
    if (this.sessionStateProvider && !this.sessionStateProvider.sessionActive) {
      const label = this.sessionStateProvider.sessionLabel;
      if (label && args.text) {
        args.text = `⚡ ${label}\n${args.text}`;
      }
    }

    // Validate file sizes
    for (const f of args.files) {
      assertSendable(f);
      const st = statSync(f);
      if (st.size > MAX_ATTACHMENT_BYTES) {
        throw new Error(
          `file too large: ${f} (${(st.size / 1024 / 1024).toFixed(1)}MB > 50MB)`,
        );
      }
    }

    // Parse reply_to as number if present
    const replyTo = args.reply_to ? Number(args.reply_to) : undefined;

    // Split text according to strategy
    const chunks =
      args.split === "newline"
        ? chunkByNewline(args.text, TG_TEXT_LIMIT)
        : chunkByLength(args.text, TG_TEXT_LIMIT);

    // Determine which chunks should reply to original message
    const useReplyTo = (i: number): boolean => {
      if (!replyTo) return false;
      if (args.reply_to_mode === "off") return false;
      if (args.reply_to_mode === "first") return i === 0;
      return true;
    };

    // Send text chunks
    const sentIds: number[] = [];
    for (let i = 0; i < chunks.length; i++) {
      const opts: { replyTo?: number; format: typeof args.format } = {
        format: args.format,
      };
      if (useReplyTo(i) && replyTo !== undefined) opts.replyTo = replyTo;
      const id = await this.tg.sendMessage(args.chat_id, chunks[i]!, opts);
      sentIds.push(id);
    }

    // Send files, optionally replying to original or first text message
    const fileReply = replyTo ?? sentIds[0];
    for (const f of args.files) {
      const id = await this.tg.sendFile(args.chat_id, f, fileReply);
      sentIds.push(id);
    }

    return {
      content: [{ type: "text", text: `sent message_ids=${sentIds.join(",")}` }],
      message_ids: sentIds,
    };
  }

  private async handleReact(rawArgs: unknown): Promise<McpResult> {
    const args = ReactArgs.parse(rawArgs);
    assertAllowedChat(this.config, args.chat_id);
    await this.tg.setReaction(args.chat_id, Number(args.message_id), args.emoji);
    return { content: [{ type: "text", text: "reaction set" }] };
  }

  private async handleEditMessage(rawArgs: unknown): Promise<McpResult> {
    const args = EditArgs.parse(rawArgs);
    assertAllowedChat(this.config, args.chat_id);
    await this.tg.editMessage(args.chat_id, Number(args.message_id), args.text, args.format);
    return { content: [{ type: "text", text: "edited" }] };
  }

  private async handleDownloadAttachment(rawArgs: unknown): Promise<McpResult> {
    const args = DownloadArgs.parse(rawArgs);
    const filePath = await this.tg.getFilePath(args.file_id);
    const localPath = await saveAttachment(this.config.botToken, args.file_id, filePath);
    return {
      content: [{ type: "text", text: `downloaded to ${localPath}` }],
    };
  }
}
