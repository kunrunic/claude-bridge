import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { statSync } from "node:fs";
import { loadConfig } from "./config.ts";
import { TelegramClient } from "./telegram/client.ts";
import { Poller } from "./telegram/poller.ts";
import { saveAttachment } from "./inbox.ts";
import { assertAllowedChat } from "./access.ts";
import {
  pendingPermissions,
  buildCompactKeyboard,
  formatCompactPrompt,
} from "./permissions.ts";
import { connectClient, type LineSocket } from "./ipc.ts";
import * as anomaly from "./anomaly.ts";
import {
  acquirePollingLock,
  installShutdownHandlers,
  releasePollingLock,
  startOrphanWatchdog,
} from "./lifecycle.ts";

const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024;
const TG_TEXT_LIMIT = 4096;

const ReplyArgs = z.object({
  chat_id: z.string(),
  text: z.string(),
  reply_to: z.string().optional(),
  files: z.array(z.string()).default([]),
  format: z.enum(["text", "markdownv2"]).default("text"),
});

const ReactArgs = z.object({
  chat_id: z.string(),
  message_id: z.string(),
  emoji: z.string(),
});

const EditArgs = z.object({
  chat_id: z.string(),
  message_id: z.string(),
  text: z.string(),
  format: z.enum(["text", "markdownv2"]).default("text"),
});

const DownloadArgs = z.object({
  file_id: z.string(),
});

function chunkByLength(s: string, limit: number): string[] {
  if (s.length <= limit) return [s];
  const out: string[] = [];
  for (let i = 0; i < s.length; i += limit) {
    out.push(s.slice(i, i + limit));
  }
  return out;
}

async function main(): Promise<void> {
  const config = loadConfig();
  const tg = new TelegramClient(config.botToken);

  const server = new Server(
    {
      name: "claude-bridge-telegram",
      version: "0.2.0",
    },
    {
      capabilities: {
        tools: {},
        experimental: {
          "claude/channel": {},
          "claude/channel/permission": {},
        },
      },
      instructions:
        "The sender reads Telegram, not this session. Anything you want them to see must go through the reply tool — your transcript output never reaches their chat. Pass chat_id and message_id (strings) from the inbound <channel> block. Use react for quick acknowledgement, edit_message for silent progress updates (no push), and download_attachment when meta has attachment_file_id.",
    },
  );

  const dispatcherSocket = process.env.CB_DISPATCHER_SOCKET;
  const sessionId = process.env.CB_SESSION_ID;
  const ipcMode = Boolean(dispatcherSocket && sessionId);

  const sendPermissionReply = (
    requestId: string,
    behavior: "allow" | "deny",
  ): void => {
    pendingPermissions.delete(requestId);
    void server
      .notification({
        method: "notifications/claude/channel/permission",
        params: { request_id: requestId, behavior },
      })
      .catch((err) => {
        anomaly.log("channel_reply_failed", {
          op: "permission_notification",
          requestId,
          error: String(err),
        });
      });
  };

  const emitInbound = (content: string, meta: Record<string, string>): void => {
    void server
      .notification({
        method: "notifications/claude/channel",
        params: { content, meta },
      })
      .catch((err) => {
        anomaly.log("channel_reply_failed", {
          op: "inbound_notification",
          error: String(err),
        });
      });
  };

  let ipc: LineSocket | undefined;
  if (ipcMode) {
    ipc = await connectClient(dispatcherSocket!);
    ipc.send({ op: "hello", session_id: sessionId!, pid: process.pid });
    ipc.onMessage((msg) => {
      switch (msg.op) {
        case "inbound":
          emitInbound(msg.content, msg.meta);
          break;
        case "permission_reply":
          sendPermissionReply(msg.request_id, msg.behavior);
          break;
        default:
          anomaly.log("mcp_unknown_method", {
            where: "server.ipc",
            op: msg.op,
          });
      }
    });
    ipc.onClose(() => {
      anomaly.log("anomaly_self_error", {
        where: "server.ipc.close",
        sessionId,
      });
    });
  }

  const poller = new Poller(
    tg,
    config,
    (evt) => emitInbound(evt.content, evt.meta as Record<string, string>),
    sendPermissionReply,
  );

  server.setNotificationHandler(
    z.object({
      method: z.literal("notifications/claude/channel/permission_request"),
      params: z.object({
        request_id: z.string(),
        tool_name: z.string(),
        description: z.string(),
        input_preview: z.string(),
      }),
    }),
    async ({ params }) => {
      pendingPermissions.set(params.request_id, {
        tool_name: params.tool_name,
        description: params.description,
        input_preview: params.input_preview,
      });
      if (ipc && sessionId) {
        ipc.send({
          op: "permission_request",
          session_id: sessionId,
          request_id: params.request_id,
          tool_name: params.tool_name,
          description: params.description,
          input_preview: params.input_preview,
        });
        return;
      }
      const keyboard = buildCompactKeyboard(params.request_id);
      const prompt = formatCompactPrompt(params.tool_name);
      for (const chatId of config.allowlist) {
        try {
          await tg.sendWithKeyboard(chatId, prompt, keyboard);
        } catch (err) {
          anomaly.log("channel_reply_failed", {
            op: "permission_request_send",
            chatId,
            requestId: params.request_id,
            error: String(err),
          });
        }
      }
    },
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: [
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
    ],
  }));

  server.setRequestHandler(CallToolRequestSchema, async (req) => {
    const name = req.params.name;
    const rawArgs = req.params.arguments ?? {};
    try {
      switch (name) {
        case "reply": {
          const args = ReplyArgs.parse(rawArgs);
          assertAllowedChat(config, args.chat_id);
          for (const f of args.files) {
            const st = statSync(f);
            if (st.size > MAX_ATTACHMENT_BYTES) {
              throw new Error(
                `file too large: ${f} (${(st.size / 1024 / 1024).toFixed(1)}MB > 50MB)`,
              );
            }
          }
          const replyTo = args.reply_to ? Number(args.reply_to) : undefined;
          const chunks = chunkByLength(args.text, TG_TEXT_LIMIT);
          const sentIds: number[] = [];
          for (let i = 0; i < chunks.length; i++) {
            const id = await tg.sendMessage(args.chat_id, chunks[i]!, {
              ...(i === 0 && replyTo ? { replyTo } : {}),
              format: args.format,
            });
            sentIds.push(id);
          }
          const fileReply = replyTo ?? sentIds[0];
          for (const f of args.files) {
            const id = await tg.sendFile(args.chat_id, f, fileReply);
            sentIds.push(id);
          }
          return {
            content: [{ type: "text", text: `sent message_ids=${sentIds.join(",")}` }],
          };
        }

        case "react": {
          const args = ReactArgs.parse(rawArgs);
          assertAllowedChat(config, args.chat_id);
          await tg.setReaction(args.chat_id, Number(args.message_id), args.emoji);
          return { content: [{ type: "text", text: "reaction set" }] };
        }

        case "edit_message": {
          const args = EditArgs.parse(rawArgs);
          assertAllowedChat(config, args.chat_id);
          await tg.editMessage(
            args.chat_id,
            Number(args.message_id),
            args.text,
            args.format,
          );
          return { content: [{ type: "text", text: "edited" }] };
        }

        case "download_attachment": {
          const args = DownloadArgs.parse(rawArgs);
          const filePath = await tg.getFilePath(args.file_id);
          const localPath = await saveAttachment(config.botToken, args.file_id, filePath);
          return {
            content: [
              { type: "text", text: `downloaded to ${localPath}` },
            ],
          };
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
  });

  const transport = new StdioServerTransport();
  await server.connect(transport);

  const pollEnabled = process.env.CB_POLL_ENABLED === "1" && !ipcMode;
  if (pollEnabled) {
    await acquirePollingLock("server.ts");
    await poller.start();
  }

  installShutdownHandlers(async (reason) => {
    anomaly.log("shutdown", { where: "server.ts", reason });
    if (pollEnabled) {
      try {
        await poller.stop();
      } catch {}
      releasePollingLock();
    }
    try {
      await transport.close();
    } catch {}
    if (ipc) ipc.close();
  });

  startOrphanWatchdog(() => {
    if (pollEnabled) releasePollingLock();
    process.exit(0);
  });
}

main().catch((err) => {
  anomaly.log("anomaly_self_error", {
    where: "server.main",
    error: String(err),
  });
  console.error(err);
  process.exit(1);
});
