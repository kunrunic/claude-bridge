import type { Registry } from "./registry.ts";
import { observe, RATE_LIMIT_RESET_RE } from "./observer.ts";
import * as anomaly from "./anomaly.ts";

const OBSERVE_TICK_MS = 5_000;

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
      } = { signal: obs.signal };
      if (obs.signal === "rate_limit" && !this.rateLimitNotified.has(s.id)) {
        this.rateLimitNotified.add(s.id);
        const resetMatch = RATE_LIMIT_RESET_RE.exec(pane);
        const resetAt = resetMatch ? ` (${resetMatch[1]})` : "";
        this.deps.announce(
          `⏸ rate limit hit: [${s.label}]${resetAt} — pressing Esc to wait`,
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
      } else if (obs.signal === "idle") {
        patch.state = "idle";
        this.rateLimitNotified.delete(s.id);
      } else if (obs.signal === "compact_error" || obs.signal === "context_limit") {
        patch.state = "error";
      }
      this.deps.registry.updateState(s.id, patch);
    }
  }
}
