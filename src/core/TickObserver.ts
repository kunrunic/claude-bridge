import type { Registry, Session } from "./registry.ts";
import { observe, RATE_LIMIT_RESET_RE } from "./observer.ts";
import * as anomaly from "./anomaly.ts";

const OBSERVE_TICK_MS = 5_000;
export const IDLE_GRACE_MS = 15_000;
export const STAGE2_DELAY_MS = 20_000;
export const REMIND_TEXT =
  "(시스템) 이전 답변이 bridge-channel 로 전송되지 않았습니다. mcp__bridge-channel__reply 도구로 사용자에게 답변을 전송해주세요.";

export type TickTmux = {
  capturePane(name: string, lines: number): string;
  sendKeys(name: string, keys: string, enter: boolean): void;
};

export type TickObserverDeps = {
  registry: Registry;
  tmux: TickTmux;
  announce: (text: string) => void;
};

export class TickObserver {
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly rateLimitNotified = new Set<string>();
  private readonly autoCompacting = new Set<string>();
  private readonly compactErrorNotified = new Set<string>();

  constructor(private readonly deps: TickObserverDeps) {}

  start(): void {
    this.timer = setInterval(() => this.tick(), OBSERVE_TICK_MS);
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  private tick(): void {
    for (const s of this.deps.registry.list()) {
      if (s.state === "dead" || s.state === "spawning") continue;
      let pane = "";
      try {
        pane = this.deps.tmux.capturePane(s.tmuxName, 40);
      } catch {
        continue;
      }
      if (!pane) continue;
      const obs = observe(pane);
      if (obs.signal !== s.signal) {
        anomaly.log("anomaly_self_error", {
          where: "TickObserver.tick",
          signalTransition: `${s.signal}→${obs.signal}`,
          sessionId: s.id,
          tailPreview: obs.lastLines.split("\n").slice(-5).join(" | ").slice(0, 300),
        });
      }
      const patch: {
        signal: typeof obs.signal;
        state?: typeof s.state;
        busySince?: number;
        idleSinceTs?: number;
      } = { signal: obs.signal };
      if (obs.signal === "rate_limit" && !this.rateLimitNotified.has(s.id)) {
        this.rateLimitNotified.add(s.id);
        const resetMatch = RATE_LIMIT_RESET_RE.exec(pane);
        const resetAt = resetMatch ? ` (${resetMatch[1]})` : "";
        this.deps.announce(
          `⏸ rate limit hit: [${s.id}][${s.label}]${resetAt} — pressing Esc to wait`,
        );
        anomaly.log("rate_limit_hit", { sessionId: s.id, label: s.label });
        try {
          this.deps.tmux.sendKeys(s.tmuxName, "Escape", false);
        } catch {
          /* ignore */
        }
        patch.state = "idle";
      } else if (obs.signal === "busy" || obs.signal === "compact") {
        patch.state = "busy";
        if (!s.busySince) patch.busySince = Date.now();
        this.autoCompacting.delete(s.id);
        this.compactErrorNotified.delete(s.id);
      } else if (obs.signal === "idle") {
        patch.state = "idle";
        if (s.state !== "idle") patch.idleSinceTs = Date.now();
        this.rateLimitNotified.delete(s.id);
        this.autoCompacting.delete(s.id);
        this.compactErrorNotified.delete(s.id);
      } else if (obs.signal === "context_limit") {
        patch.state = "error";
        if (!this.autoCompacting.has(s.id)) {
          this.autoCompacting.add(s.id);
          this.deps.announce(
            `⚠️ context limit: [${s.id}][${s.label}] — /compact 자동 실행`,
          );
          anomaly.log("anomaly_self_error", {
            where: "TickObserver.tick",
            event: "auto_compact",
            sessionId: s.id,
          });
          try {
            this.deps.tmux.sendKeys(s.tmuxName, "/compact", true);
          } catch {
            /* ignore */
          }
        }
      } else if (obs.signal === "compact_error") {
        patch.state = "error";
        if (!this.compactErrorNotified.has(s.id)) {
          this.compactErrorNotified.add(s.id);
          this.deps.announce(
            `❌ compact 실패: [${s.id}][${s.label}] — /model 로 모델 전환 후 재시도하세요`,
          );
          anomaly.log("compact_error", { sessionId: s.id, label: s.label });
        }
      }
      this.deps.registry.updateState(s.id, patch);
      this.checkMissingReply(s.id);
    }
  }

  // Detect the case where Claude returned to idle without sending a reply via
  // mcp__bridge-channel__reply. Two-stage escalation:
  //  1. silent tmux sendKeys reminder to Claude (induces MCP tool use)
  //  2. if still unreplied after STAGE2_DELAY_MS, notify user on Telegram
  private checkMissingReply(sessionId: string): void {
    const s: Session | undefined = this.deps.registry.get(sessionId);
    if (!s) return;
    if (s.state !== "idle") return;
    if (!s.inboundAt) return;
    if ((s.replySentAt ?? 0) >= s.inboundAt) return;
    // idle must have started AFTER inbound — otherwise a stale idleSinceTs
    // from a previous turn (e.g. yesterday) would trigger immediately when
    // today's new inbound arrives before Claude has had a chance to respond.
    if (!s.idleSinceTs || s.idleSinceTs <= s.inboundAt) return;
    const now = Date.now();
    if (now - s.idleSinceTs < IDLE_GRACE_MS) return;

    if ((s.remindSentAt ?? 0) < s.inboundAt) {
      try {
        // Split text and Enter into two tmux commands — sending them together
        // can get batched into bracketed-paste mode where Enter becomes a
        // literal newline instead of submit.
        this.deps.tmux.sendKeys(s.tmuxName, REMIND_TEXT, false);
        this.deps.tmux.sendKeys(s.tmuxName, "Enter", false);
      } catch (err) {
        anomaly.log("reply_missing_remind_failed", {
          sessionId: s.id,
          error: String(err),
        });
        return;
      }
      anomaly.log("reply_missing_remind", {
        sessionId: s.id,
        inboundAt: s.inboundAt,
        idleSinceTs: s.idleSinceTs,
      });
      this.deps.registry.updateState(s.id, { remindSentAt: now });
      return;
    }

    if (
      (s.userNoticeSentAt ?? 0) < s.inboundAt &&
      now - (s.remindSentAt ?? 0) >= STAGE2_DELAY_MS
    ) {
      this.deps.announce(
        `⚠️ [${s.id}][${s.label}] 답변이 누락됐습니다. 다시 요청해주세요.`,
      );
      anomaly.log("reply_missing_user_notice", {
        sessionId: s.id,
        inboundAt: s.inboundAt,
        remindSentAt: s.remindSentAt,
      });
      this.deps.registry.updateState(s.id, { userNoticeSentAt: now });
    }
  }
}
