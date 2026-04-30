import { loadConfig } from "../src/channels/telegram/config.ts";
import { TelegramClient } from "../src/channels/telegram/client.ts";

const config = loadConfig();
if (!config.defaultChatId) {
  throw new Error("defaultChatId missing in config");
}
if (!config.botToken) {
  throw new Error("botToken missing — telegram smoke requires Telegram config");
}
const tg = new TelegramClient(config.botToken);
const id = await tg.sendMessage(
  config.defaultChatId,
  "[channels/telegram stage1] hello — outbound reply path OK",
);
console.log("sent message_id=", id);
