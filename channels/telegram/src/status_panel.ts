import type { TelegramClient } from "./telegram/client.ts";
import type { Registry, Session } from "./registry.ts";
import * as anomaly from "./anomaly.ts";

const DEBOUNCE_MS = 250;

function stateIcon(s: Session): string {
  if (s.state === "spawning") return "⏳";
  if (s.state === "dead" || s.state === "error") return "🛑";
  if (s.state === "busy") return "🔔";
  return "▶";
}

function durSince(ts?: number): string {
  if (!ts) return "";
  const sec = Math.floor((Date.now() - ts) / 1000);
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  return `${Math.floor(sec / 3600)}h`;
}

export function renderPanel(registry: Registry): string {
  const sessions = registry.list();
  if (sessions.length === 0) return "📋 Sessions\n  (none — /new to start)";
  const active = registry.active();
  const lines = ["📋 Sessions"];
  for (const s of sessions) {
    const isActive = active && active.id === s.id;
    const prefix = isActive ? "▶" : stateIcon(s);
    const activeMark = isActive ? " (active)" : "";
    const busyBit =
      s.state === "busy" && s.busySince
        ? ` · ⏳ ${durSince(s.busySince)}`
        : s.lastReplyTs
          ? ` · ${durSince(s.lastReplyTs)} ago`
          : "";
    lines.push(`  ${prefix} ${s.label}${activeMark}${busyBit}`);
  }
  return lines.join("\n");
}

export class StatusPanel {
  private chatId: string | undefined;
  private messageId: number | undefined;
  private pending: ReturnType<typeof setTimeout> | undefined;
  private lastText = "";

  constructor(
    private readonly tg: TelegramClient,
    private readonly registry: Registry,
  ) {}

  async ensure(chatId: string): Promise<void> {
    if (this.chatId === chatId && this.messageId) return;
    this.chatId = chatId;
    const text = renderPanel(this.registry);
    this.lastText = text;
    try {
      this.messageId = await this.tg.sendMessage(chatId, text);
      try {
        await this.tg.bot.api.pinChatMessage(chatId, this.messageId, {
          disable_notification: true,
        });
      } catch {
        // pin may fail in private chats for some configs — non-fatal
      }
    } catch (err) {
      anomaly.log("status_panel_edit_failed", {
        op: "ensure.send",
        error: String(err),
      });
    }
  }

  touch(): void {
    if (this.pending) return;
    this.pending = setTimeout(() => {
      this.pending = undefined;
      void this.flush();
    }, DEBOUNCE_MS);
  }

  private async flush(): Promise<void> {
    if (!this.chatId || !this.messageId) return;
    const text = renderPanel(this.registry);
    if (text === this.lastText) return;
    try {
      await this.tg.editMessage(this.chatId, this.messageId, text);
      this.lastText = text;
    } catch (err) {
      anomaly.log("status_panel_edit_failed", {
        error: String(err),
      });
    }
  }
}
