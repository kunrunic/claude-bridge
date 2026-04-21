import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { loadConfig } from "./config.ts";
import { TelegramClient } from "./client.ts";
import { Poller } from "./poller.ts";
import {
  pendingPermissions,
  buildCompactKeyboard,
  formatCompactPrompt,
} from "./permissions.ts";
import { ToolHandler } from "./tools/ToolHandler.ts";
import { TOOL_LIST } from "./tools/definitions.ts";
import { IpcBridge } from "./IpcBridge.ts";
import * as anomaly from "../../core/anomaly.ts";
import {
  acquirePollingLock,
  installShutdownHandlers,
  releasePollingLock,
  startOrphanWatchdog,
} from "../../core/lifecycle.ts";

// Re-export text utilities for backwards compatibility
export {
  chunkByLength,
  chunkByNewline,
  assertSendable,
  TG_TEXT_LIMIT,
  MAX_ATTACHMENT_BYTES,
} from "./tools/text.ts";

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
  anomaly.log("server_startup", {
    where: "server.main",
    ipcMode,
    hasSocket: Boolean(dispatcherSocket),
    hasSessionId: Boolean(sessionId),
    sessionId: sessionId ?? null,
    pid: process.pid,
    ppid: process.ppid,
  });

  // Set up MCP notification handlers
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

  // Set up IPC bridge if in dispatcher mode
  const ipcBridge = new IpcBridge();
  if (ipcMode) {
    await ipcBridge.connect(dispatcherSocket!, sessionId!, process.pid, {
      emitInbound,
      sendPermissionReply,
      onClose: () => {},
      onReconnecting: (attempt, max) => {
        anomaly.log("anomaly_self_error", {
          where: "IpcBridge.reconnecting",
          attempt,
          max,
          sessionId,
        });
      },
      onReconnected: () => {
        anomaly.log("anomaly_self_error", {
          where: "IpcBridge.reconnected",
          sessionId,
        });
      },
      onReconnectFailed: () => {
        anomaly.log("anomaly_self_error", {
          where: "IpcBridge.reconnect_failed",
          sessionId,
        });
      },
    });
  }

  const poller = new Poller(
    tg,
    config,
    (evt) => emitInbound(evt.content, evt.meta as Record<string, string>),
    sendPermissionReply,
  );

  // Permission request handler
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
      if (ipcBridge.connected && sessionId) {
        ipcBridge.send({
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

  // List tools handler
  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: TOOL_LIST,
  }));

  // Call tool handler
  const toolHandler = new ToolHandler(tg, config, ipcMode ? ipcBridge : undefined);
  server.setRequestHandler(CallToolRequestSchema, async (req) => {
    const name = req.params.name;
    const rawArgs = req.params.arguments ?? {};
    const result = await toolHandler.handle(name, rawArgs);
    const isUserFacing = name === "reply" || name === "react" || name === "edit_message";
    if (isUserFacing && !result.isError && ipcMode && sessionId) {
      const payload: { op: "reply_sent"; session_id: string; message_ids?: number[] } = {
        op: "reply_sent",
        session_id: sessionId,
      };
      if (result.message_ids && result.message_ids.length > 0) {
        payload.message_ids = result.message_ids;
      }
      ipcBridge.send(payload);
    }
    return result;
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
    ipcBridge.close();
  });

  startOrphanWatchdog(() => {
    if (pollEnabled) releasePollingLock();
    process.exit(0);
  });
}

if (import.meta.main) {
  main().catch((err) => {
    anomaly.log("anomaly_self_error", {
      where: "server.main",
      error: String(err),
    });
    console.error(err);
    process.exit(1);
  });
}
