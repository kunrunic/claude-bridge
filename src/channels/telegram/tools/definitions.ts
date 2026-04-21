import { z } from "zod";

/**
 * Zod schema for the reply tool arguments.
 */
export const ReplyArgs = z.object({
  chat_id: z.string(),
  text: z.string(),
  reply_to: z.string().optional(),
  reply_to_mode: z.enum(["off", "first", "all"]).default("all"),
  files: z.array(z.string()).default([]),
  format: z.enum(["text", "markdownv2"]).default("text"),
  split: z.enum(["newline", "length"]).default("newline"),
});

/**
 * Zod schema for the react tool arguments.
 */
export const ReactArgs = z.object({
  chat_id: z.string(),
  message_id: z.string(),
  emoji: z.string(),
});

/**
 * Zod schema for the edit_message tool arguments.
 */
export const EditArgs = z.object({
  chat_id: z.string(),
  message_id: z.string(),
  text: z.string(),
  format: z.enum(["text", "markdownv2"]).default("text"),
});

/**
 * Zod schema for the download_attachment tool arguments.
 */
export const DownloadArgs = z.object({
  file_id: z.string(),
});

/**
 * Tool list for ListToolsRequestSchema.
 * Exported as a constant so it can be reused and tested in isolation.
 */
export const TOOL_LIST = [
  {
    name: "reply",
    description:
      "Reply on Telegram. Pass chat_id from the inbound message. reply_to (message_id) for threading, files (abs paths) for attachments.",
    inputSchema: {
      type: "object",
      properties: {
        chat_id: { type: "string" },
        text: { type: "string" },
        reply_to: { type: "string" },
        reply_to_mode: {
          type: "string",
          enum: ["off", "first", "all"],
          description:
            "How reply_to applies to split chunks. off=no threading, first=chunk 1 only, all=every chunk quotes original (default; best for multi-turn context).",
        },
        files: {
          type: "array",
          items: { type: "string" },
          description: "Absolute file paths. Images → photo, others → document. Max 50MB each.",
        },
        format: {
          type: "string",
          enum: ["text", "markdownv2"],
          description: "Rendering mode. Default text.",
        },
        split: {
          type: "string",
          enum: ["newline", "length"],
          description: "Chunk strategy for >4096 char text. newline=prefer line boundaries (default), length=hard cut.",
        },
      },
      required: ["chat_id", "text"],
    },
  },
  {
    name: "react",
    description:
      "Add an emoji reaction to a Telegram message (fixed whitelist: 👍 👎 ❤ 🔥 👀 🎉 etc).",
    inputSchema: {
      type: "object",
      properties: {
        chat_id: { type: "string" },
        message_id: { type: "string" },
        emoji: { type: "string" },
      },
      required: ["chat_id", "message_id", "emoji"],
    },
  },
  {
    name: "edit_message",
    description:
      "Edit a message the bot previously sent. Silent — no push notification. Use for progress updates; send a new reply() when work completes.",
    inputSchema: {
      type: "object",
      properties: {
        chat_id: { type: "string" },
        message_id: { type: "string" },
        text: { type: "string" },
        format: { type: "string", enum: ["text", "markdownv2"] },
      },
      required: ["chat_id", "message_id", "text"],
    },
  },
  {
    name: "download_attachment",
    description:
      "Download a Telegram file to the local inbox. Use when inbound meta has attachment_file_id. Returns local path.",
    inputSchema: {
      type: "object",
      properties: {
        file_id: { type: "string" },
      },
      required: ["file_id"],
    },
  },
];
