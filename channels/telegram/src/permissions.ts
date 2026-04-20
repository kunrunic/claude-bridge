import { InlineKeyboard } from "grammy";

export type PermissionDetails = {
  tool_name: string;
  description: string;
  input_preview: string;
};

export const pendingPermissions = new Map<string, PermissionDetails>();

export const CALLBACK_RE = /^perm:(allow|deny|more):([a-km-z]{5})$/;
export const REPLY_RE = /^\s*(y|yes|n|no)\s+([a-km-z]{5})\s*$/i;

export function buildCompactKeyboard(requestId: string): InlineKeyboard {
  return new InlineKeyboard()
    .text("See more", `perm:more:${requestId}`)
    .text("✅ Allow", `perm:allow:${requestId}`)
    .text("❌ Deny", `perm:deny:${requestId}`);
}

export function buildExpandedKeyboard(requestId: string): InlineKeyboard {
  return new InlineKeyboard()
    .text("✅ Allow", `perm:allow:${requestId}`)
    .text("❌ Deny", `perm:deny:${requestId}`);
}

export function formatExpandedBody(details: PermissionDetails): string {
  let prettyInput: string;
  try {
    prettyInput = JSON.stringify(JSON.parse(details.input_preview), null, 2);
  } catch {
    prettyInput = details.input_preview;
  }
  return (
    `🔐 Permission: ${details.tool_name}\n\n` +
    `tool_name: ${details.tool_name}\n` +
    `description: ${details.description}\n` +
    `input_preview:\n${prettyInput}`
  );
}

export function formatCompactPrompt(toolName: string): string {
  return `🔐 Permission: ${toolName}`;
}
