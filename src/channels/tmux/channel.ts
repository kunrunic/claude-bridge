/**
 * TmuxStatusChannel — Channel 인터페이스의 tmux status bar 구현체.
 *
 * 각 cb-* tmux 세션의 status-right 영역에 미니맵을 표시한다. 사용자가
 * SSH 등으로 그 tmux 세션에 attach 한 상태에서, status bar 만 봐도 다른
 * 세션의 상태를 확인할 수 있게 함.
 *
 * 입력 채널이 없는 (출력 전용) 채널 — 사용자 입력은 Phase 3 의 cb CLI 가
 * 별도 Unix 소켓으로 처리. ChannelDeps 의 onInbound / onPermissionReply 는
 * 인터페이스 일치를 위해 받지만 호출하지 않는다.
 *
 * 디자인 결정:
 *  - announce / requestPermission 은 status bar 와 어울리지 않음 → 무시.
 *    Telegram 채널이 동시 운영 중이면 거기서 사용자에게 표시됨. CLI-only
 *    운영 모드 (Phase 4) 에서는 Phase 3 의 cb CLI 가 같은 콜백 등록 받아 처리.
 *  - 미니맵에 perm-wait 표시되므로 다른 세션의 권한 요청 인지는 가능.
 */

import type { Channel, ChannelDeps, PermissionRequest, SessionEvent } from "../../core/channel.ts";
import type { Registry, Session } from "../../core/registry.ts";
import * as status from "../../core/tmux/status.ts";
import { renderMinimap, hasAnimatedSignal, SPINNER_FRAMES } from "./render.ts";

const STATUS_RIGHT_LENGTH = 200;
// busy / spawning 세션이 있을 때만 200ms 간격으로 minimap 재그림. 모두 idle 이면
// timer 가 호출돼도 lastRender cache 가 동일 텍스트를 반환하므로 set-option 안 됨.
const SPIN_INTERVAL_MS = 200;

export type TmuxStatusChannelDeps = ChannelDeps & {
  registry: Registry;
};

export class TmuxStatusChannel implements Channel {
  readonly name = "tmux";

  // 이전에 갱신한 status-right 텍스트 — 동일하면 set-option 호출 회피.
  // state_changed 가 매 tick(~500ms) 발행될 수 있어 캐시가 의미 있음.
  private readonly lastRender = new Map<string, string>();
  // status on / status-right-length 는 세션당 1회만 설정.
  private readonly initialized = new Set<string>();
  // spinner frame index — busy/spawning 세션이 있을 때 200ms 마다 회전.
  private spinFrame = 0;
  private spinTimer: ReturnType<typeof setInterval> | undefined;

  constructor(private readonly deps: TmuxStatusChannelDeps) {}

  // ── Channel: lifecycle ────────────────────────────────────────────────────

  async start(): Promise<void> {
    // dispatcher 시작 시점엔 보통 세션이 없지만, 재시작 후 registry 가 미리
    // 로드된 경우엔 한 번 갱신. 실제로는 reconcileOrphans 가 모두 비워서 no-op.
    this.refreshAll();
    this.spinTimer = setInterval(() => this.spinTick(), SPIN_INTERVAL_MS);
  }

  async stop(): Promise<void> {
    if (this.spinTimer) {
      clearInterval(this.spinTimer);
      this.spinTimer = undefined;
    }
    // tmux 세션이 종료되면 status-right 도 같이 사라지므로 별도 cleanup 불필요.
  }

  /** 200ms 마다 호출 — busy/spawning 세션 있으면 frame 증가 후 minimap refresh. */
  private spinTick(): void {
    const all = this.deps.registry.list();
    if (!hasAnimatedSignal(all)) return;
    this.spinFrame = (this.spinFrame + 1) % SPINNER_FRAMES.length;
    this.refreshAll();
  }

  // ── Channel: outbound ─────────────────────────────────────────────────────

  notify(evt: SessionEvent): void {
    switch (evt.type) {
      case "spawned":
      case "killed":
      case "active_changed":
      case "active_cleared":
      case "state_changed":
      case "disconnected":
      case "reconnected":
        this.refreshAll();
        break;
      case "inbound_delivered":
      case "reply_sent":
        // signal 변화는 직후의 state_changed 로 별도 갱신 트리거된다 — 중복 회피.
        break;
    }
  }

  announce(_text: string): void {
    // status bar 매체엔 어울리지 않음 — 다른 채널에서 표시.
  }

  requestPermission(_req: PermissionRequest): void {
    // status bar 에 prompt 띄울 수 없음. 사용자는 active 세션에 attach 했을 때
    // Claude TUI 자체의 권한 prompt 를 본다. 다른 세션의 권한 대기는 미니맵의
    // ⚠ sigil 로 인지 가능.
  }

  // ── 내부 ──────────────────────────────────────────────────────────────────

  /** 모든 등록된 세션의 status-right 갱신. dead 캐시도 정리. */
  private refreshAll(): void {
    const all = this.deps.registry.list();
    const aliveNames = new Set<string>();
    for (const s of all) {
      aliveNames.add(s.tmuxName);
      this.refreshOne(s.tmuxName, s.id, all);
    }
    for (const name of [...this.lastRender.keys()]) {
      if (!aliveNames.has(name)) {
        this.lastRender.delete(name);
        this.initialized.delete(name);
      }
    }
  }

  private refreshOne(tmuxName: string, viewerSessionId: string, all: Session[]): void {
    const spin = SPINNER_FRAMES[this.spinFrame] ?? SPINNER_FRAMES[0];
    const text = renderMinimap(all, viewerSessionId, spin);
    if (this.lastRender.get(tmuxName) === text) return;

    if (!this.initialized.has(tmuxName)) {
      status.enableStatus(tmuxName);
      status.setStatusPosition(tmuxName, "top");
      status.setStatusLines(tmuxName, 2);
      status.setStatusRightLength(tmuxName, STATUS_RIGHT_LENGTH);
      // bg=black,fg=white 로 통일 — tmux default(bg=green,fg=black) 는 minimap 글자가
      // background 와 비슷해 보이지 않는다.
      status.setStatusStyle(tmuxName, "bg=black,fg=white");
      // 둘 다 우리가 명시 — default status-format[0] 은 list/style 가 복잡해 minimap 이
      // 안 보이는 환경이 있어 단순 한 줄로 강제.
      // status-format[0] 안의 #{status-right} 는 setStatusRight 으로 갱신되는 minimap 텍스트.
      status.setStatusFormat(tmuxName, 0, "#[align=right]#{status-right}");
      status.setStatusFormat(
        tmuxName,
        1,
        "#[align=right]F1 sessions  F2 new  F3/F4 prev/next  F5 disconnect  F6 handoff",
      );
      // Claude Code 가 keyboard focus 추적용으로 focus-events 사용 — default off 라
      // 시작 시 안내 메시지 출력. 켜두면 깔끔.
      status.setOption(tmuxName, "focus-events", "on");
      this.initialized.add(tmuxName);
    }
    status.setStatusRight(tmuxName, text);
    this.lastRender.set(tmuxName, text);
  }
}
