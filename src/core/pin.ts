import type { Registry } from "./registry.ts";
import * as anomaly from "./anomaly.ts";

export type PinTelegramDeps = {
  sendMessage: (chatId: string, text: string) => Promise<number>;
  editMessage: (chatId: string, msgId: number, text: string) => Promise<void>;
  pinMessage: (chatId: string, msgId: number, silent?: boolean) => Promise<void>;
  unpinMessage: (chatId: string, msgId: number) => Promise<void>;
};

export async function updateActivePin(
  registry: Registry,
  tg: PinTelegramDeps,
  chatId: string,
  pendingCounts?: Map<string, number>,
): Promise<void> {
  const active = registry.active();
  const currentPin = registry.getActivePin();

  const hasPending = pendingCounts
    ? [...pendingCounts.entries()].some(([, n]) => n > 0)
    : false;

  if (!active && !hasPending) {
    if (currentPin) {
      await tg.unpinMessage(currentPin.chatId, currentPin.messageId).catch(() => {});
      registry.setActivePin(undefined);
    }
    return;
  }

  const lines: string[] = [];
  lines.push(active ? `⚡ active: ${active.id} (${active.label})` : "⚡ no active session");
  if (pendingCounts) {
    const pending = [...pendingCounts.entries()]
      .filter(([, n]) => n > 0)
      .sort(([a], [b]) => a.localeCompare(b));
    for (const [sid, count] of pending) {
      lines.push(`📬 ${sid}: ${count}`);
    }
  }
  const text = lines.join("\n");

  // Try to edit an existing pin message in this chat — avoids spamming a new
  // message every time the pin content changes.
  if (currentPin && currentPin.chatId === chatId) {
    try {
      await tg.editMessage(chatId, currentPin.messageId, text);
      return;
    } catch (err) {
      // Telegram returns "message is not modified" when the new text equals
      // the existing one. That's a success case — the pin already shows what
      // we want. Do NOT fall through to create a new pin.
      const msg = String(err);
      if (msg.includes("message is not modified")) {
        return;
      }
      // Otherwise (message deleted, too old, etc.) fall through to create new.
    }
  }

  try {
    const newId = await tg.sendMessage(chatId, text);
    await tg.pinMessage(chatId, newId, true);
    if (currentPin && (currentPin.chatId !== chatId || currentPin.messageId !== newId)) {
      await tg.unpinMessage(currentPin.chatId, currentPin.messageId).catch(() => {});
    }
    registry.setActivePin({ chatId, messageId: newId });
  } catch (err) {
    anomaly.log("telegram_api_failed", { op: "updateActivePin", error: String(err) });
  }
}
