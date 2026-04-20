import { loadConfig } from "../src/config.ts";
import { TelegramClient } from "../src/telegram/client.ts";

const config = loadConfig();
if (!config.defaultChatId) {
  throw new Error("defaultChatId missing in config");
}
const tg = new TelegramClient(config.botToken);
const id = await tg.sendMessage(
  config.defaultChatId,
  "[channels/telegram stage1] hello — outbound reply path OK",
);
console.log("sent message_id=", id);
