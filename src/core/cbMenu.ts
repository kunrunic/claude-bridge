/**
 * cb-menu supervisor.
 *
 * dispatcher 가 startup 시 영속 tmux session (`cb-menu` 또는 `cb-<inst>-menu`)
 * 을 만들고 그 안에 ink 기반 menu TUI 를 띄운다. 사용자가 ssh 진입 시 직접
 * 이 session 에 attach → 메뉴 안에서 다른 cb-* session 으로 switch-client.
 *
 * 이 supervisor 는:
 *  - startup 에 session 이 없으면 spawn
 *  - 1초마다 has-session 폴링 → 죽었으면 재spawn (ink crash / pane exit 대비)
 *  - shutdown 시 stop() 으로 polling 중단 + kill-session
 *
 * 기존 cb-claude-* session lifecycle (SessionManager) 와 분리 — registry 에
 * 등록되지 않으며, tick observer / channel notify 영향도 받지 않음.
 */

import { hasSession, newSession, killSession } from "./tmux/session.ts";
import * as anomaly from "./anomaly.ts";

const POLL_MS = 1000;

export type CbMenuOptions = {
  tmuxName: string;
  /** bun run 으로 실행할 진입점 절대 경로 (.tsx) */
  entry: string;
  socketPath: string;
};

export class CbMenuSupervisor {
  private alive = false;
  private timer?: ReturnType<typeof setTimeout>;

  constructor(private readonly opts: CbMenuOptions) {}

  start(): void {
    this.alive = true;
    this.respawn();
    this.scheduleWatch();
  }

  stop(): void {
    this.alive = false;
    if (this.timer) clearTimeout(this.timer);
    try {
      killSession(this.opts.tmuxName);
    } catch {
      // best-effort
    }
  }

  /** 테스트 hook — 외부에서 강제 점검 트리거. */
  tick(): void {
    if (!this.alive) return;
    if (!hasSession(this.opts.tmuxName)) this.respawn();
  }

  private respawn(): void {
    if (hasSession(this.opts.tmuxName)) return;
    try {
      newSession({
        name: this.opts.tmuxName,
        // tmux 가 /bin/sh -c <command> 로 실행 — 경로에 공백 있을 수 있어 quote.
        command: `bun run ${shQuote(this.opts.entry)}`,
        env: {
          CB_DISPATCHER_SOCKET: this.opts.socketPath,
          CB_MENU: "1",
          TERM: process.env.TERM ?? "xterm-256color",
        },
      });
    } catch (err) {
      anomaly.log("cb_menu_spawn_failed", {
        where: "CbMenuSupervisor.respawn",
        error: String(err),
      });
    }
  }

  private scheduleWatch(): void {
    if (!this.alive) return;
    this.timer = setTimeout(() => {
      if (this.alive && !hasSession(this.opts.tmuxName)) {
        this.respawn();
      }
      this.scheduleWatch();
    }, POLL_MS);
  }
}

function shQuote(p: string): string {
  return `"${p.replace(/(["\\$`])/g, "\\$1")}"`;
}
