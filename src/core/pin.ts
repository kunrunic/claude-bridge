import type { Registry } from "./registry.ts";

export type PinTelegramDeps = {
  sendMessage: (chatId: string, text: string) => Promise<number>;
  editMessage: (chatId: string, msgId: number, text: string) => Promise<void>;
  pinMessage: (chatId: string, msgId: number, silent?: boolean) => Promise<void>;
  unpinMessage: (chatId: string, msgId: number) => Promise<void>;
};

// Unpin the current active pin when there's no active session.
// Active-session pinning is handled by switchActivePin in dispatcher (session switch).
export async function updateActivePin(
  registry: Registry,
  tg: PinTelegramDeps,
  _chatId: string,
): Promise<void> {
  if (registry.active()) return;
  const currentPin = registry.getActivePin();
  if (currentPin) {
    await tg.unpinMessage(currentPin.chatId, currentPin.messageId).catch(() => {});
    registry.setActivePin(undefined);
  }
}
