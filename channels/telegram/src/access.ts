import type { Config } from "./config.ts";

export type AccessDecision =
  | { action: "allow" }
  | { action: "drop"; reason: string };

export function gate(config: Config, chatId: string, userId: string): AccessDecision {
  if (config.allowlist.length === 0) {
    return { action: "drop", reason: "allowlist empty" };
  }
  if (config.allowlist.includes(userId)) {
    return { action: "allow" };
  }
  if (config.allowlist.includes(chatId)) {
    return { action: "allow" };
  }
  return { action: "drop", reason: `user_id ${userId} not in allowlist` };
}

export function assertAllowedChat(config: Config, chatId: string): void {
  if (!config.allowlist.includes(chatId) && config.defaultChatId !== chatId) {
    throw new Error(`chat_id ${chatId} not in allowlist`);
  }
}
