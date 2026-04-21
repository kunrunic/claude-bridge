import type { Registry } from "./registry.ts";
import * as anomaly from "./anomaly.ts";

export type PinTelegramDeps = {
  sendMessage: (chatId: string, text: string) => Promise<number>;
  pinMessage: (chatId: string, msgId: number, silent?: boolean) => Promise<void>;
  unpinMessage: (chatId: string, msgId: number) => Promise<void>;
};

export async function updateActivePin(
  registry: Registry,
  tg: PinTelegramDeps,
  chatId: string,
): Promise<void> {
  const active = registry.active();
  const currentPin = registry.getActivePin();

  if (!active) {
    if (currentPin) {
      await tg.unpinMessage(currentPin.chatId, currentPin.messageId).catch(() => {});
      registry.setActivePin(undefined);
    }
    return;
  }

  const text = `🧷 active: ${active.id} (${active.label})`;
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
