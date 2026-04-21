import { InlineKeyboard } from "grammy";
import type { TelegramClient } from "./client.ts";
import type { Registry, Session, SessionState } from "../../core/registry.ts";
import type { SessionManager } from "../../core/SessionManager.ts";
import type { SessionAction } from "./poller.ts";
import type { Signal } from "../../core/observer.ts";
import * as core from "../../core/dispatcher-core.ts";
import * as slash from "../../core/slash.ts";

// ── session button formatting ─────────────────────────────────────────────────

function stateIcon(state: SessionState, signal: Signal): string {
  if (state === "spawning") return "🌀";
  if (state === "dead")     return "⚫";
  if (state === "error")    return "🔴";
  if (signal === "rate_limit") return "⏸";
  if (signal === "busy" || signal === "compact") return "🔵";
  return "🟢";
}

function fmtLabel(label: string, maxLen = 10): string {
  return label.length > maxLen ? label.slice(0, maxLen - 1) + "…" : label;
}

function fmtSessionBtn(s: Session, idx: number, isActive: boolean): string {
  const mark   = isActive ? "▶" : " ";
  const idxStr = String(idx).padStart(3, " ");
  const icon   = stateIcon(s.state, s.signal);
  return `${mark}${idxStr} ${icon} ${fmtLabel(s.label)}`;
}

export type SlashHandlerDeps = {
  registry: Registry;
  tg: TelegramClient;
  sessions: SessionManager;
  doUpdateActivePin: () => Promise<void>;
};

export class SlashHandler {
  constructor(private readonly deps: SlashHandlerDeps) {}

  async handle(cmd: slash.SlashCommand, chatId: string): Promise<void> {
    const { registry, tg, sessions } = this.deps;
    const beforeActiveId = registry.active()?.id;

    if (cmd.kind === "sessions") {
      const list = registry.list();
      if (list.length === 0) {
        await tg.sendMessage(chatId, "no sessions. /new to start.").catch(() => {});
        return;
      }
      const active = registry.active();
      const kb = new InlineKeyboard();
      list.forEach((s, i) => {
        const isActive = active?.id === s.id;
        kb.text(fmtSessionBtn(s, i + 1, isActive), `switch:${s.id}`).row();
      });
      kb.text("✖ cancel", "cancel:").row();
      await tg
        .sendWithKeyboard(chatId, "세션 선택 (탭하면 전환):", kb)
        .catch(() => {});
      return;
    }

    if (cmd.kind === "kill" && !cmd.target) {
      const list = registry.list();
      if (list.length === 0) {
        await tg.sendMessage(chatId, "no sessions to kill").catch(() => {});
        return;
      }
      const activeId = registry.active()?.id;
      const kb = new InlineKeyboard();
      list.forEach((s, i) => {
        const isActive = s.id === activeId;
        kb.text(`🗑 ${fmtSessionBtn(s, i + 1, isActive)}`, `kill:${s.id}`).row();
      });
      kb.text("✖ cancel", "cancel:").row();
      await tg
        .sendWithKeyboard(chatId, "종료할 세션 선택:", kb)
        .catch(() => {});
      return;
    }

    if (cmd.kind === "new") {
      const kb = new InlineKeyboard();
      kb.text("🔒 권한 확인 포함", "new_normal:").row();
      kb.text("🔴 권한 확인 스킵", "new_skip:").row();
      kb.text("✖ cancel", "cancel:").row();
      await tg.sendWithKeyboard(chatId, "새 세션 권한 설정:", kb).catch(() => {});
      return;
    }

    if ((cmd.kind === "resume" || cmd.kind === "fork") && !cmd.target) {
      const cache = sessions.refreshPickerCache();
      if (cache.length === 0) {
        await tg.sendMessage(chatId, "no recent sessions found").catch(() => {});
        return;
      }
      const verb = cmd.kind;
      const kb = new InlineKeyboard();
      cache.forEach((s, i) => {
        const idxStr = String(i + 1).padStart(3, " ");
        const proj  = fmtLabel(s.project, 10);
        const title = fmtLabel(s.title, 12);
        kb.text(`${idxStr} [${s.mtimeText}] ${proj} · ${title}`, `${verb}:${s.id}`).row();
      });
      kb.text("✖ cancel", "cancel:").row();
      await tg
        .sendWithKeyboard(chatId, `${verb === "resume" ? "복원" : "포크"} 세션 선택:`, kb)
        .catch(() => {});
      return;
    }

    const coreDeps: core.HandleSlashDeps = {
      registry,
      spawn: (opts) => sessions.spawn(opts),
      resume: (target, fork) => sessions.resume(target, fork),
      listRecent: () => sessions.listRecent(),
      kill: (target) => sessions.kill(target),
    };

    const reply = core.handleSlash(coreDeps, cmd);
    void tg.sendMessage(chatId, reply).catch(() => {});

    if (registry.active()?.id !== beforeActiveId) {
      void this.deps.doUpdateActivePin();
    }
  }

  async onSessionAction(
    action: SessionAction,
    target: string,
    chatId: string,
    msgId: number,
  ): Promise<void> {
    const { registry, tg, sessions } = this.deps;

    if (action === "cancel") {
      await tg
        .editWithKeyboard(chatId, msgId, "✖ cancelled")
        .catch(() => {});
      return;
    }

    const beforeActiveId = registry.active()?.id;
    let text: string;

    if (action === "new_normal" || action === "new_skip") {
      const skipPermissions = action === "new_skip";
      try {
        const r = sessions.spawn({ skipPermissions });
        text = `spawning ${r.label}...`;
      } catch (err) {
        text = `spawn failed: ${String(err)}`;
      }
    } else if (action === "kill") {
      const ok = sessions.kill(target);
      text = ok
        ? `🗑 killed ${target}`
        : `no such session: ${target}`;
    } else if (action === "switch") {
      const s = registry.get(target) ?? registry.getByLabel(target);
      if (!s) {
        text = `no such session: ${target}`;
      } else {
        registry.setActive(s.id);
        const missed = s.backlog.length;
        text = `▶ switched to ${s.label}${missed ? ` · ${missed} backlog msg(s)` : ""}`;
      }
    } else if (action === "resume" || action === "fork") {
      // two-step: show permission picker before spawning
      const verb = action;
      const kb = new InlineKeyboard();
      kb.text("🔒 권한 확인 포함", `${verb}_normal:${target}`).row();
      kb.text("🔴 권한 확인 스킵", `${verb}_skip:${target}`).row();
      kb.text("✖ cancel", "cancel:").row();
      await tg.editWithKeyboard(chatId, msgId, `권한 설정 (${verb}):`, kb).catch(() => {});
      return;
    } else {
      // resume_normal, resume_skip, fork_normal, fork_skip
      const isFork = action.startsWith("fork");
      const skipPermissions = action.endsWith("_skip");
      text = sessions.resume(target, isFork, skipPermissions);
    }

    await tg.editWithKeyboard(chatId, msgId, text).catch(() => {});

    if (registry.active()?.id !== beforeActiveId) {
      void this.deps.doUpdateActivePin();
    }
  }
}
