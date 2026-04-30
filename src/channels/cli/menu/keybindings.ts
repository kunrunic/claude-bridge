/**
 * cb-menu 가 mount 되기 직전 1회 셋업하는 tmux 키바인딩.
 *
 * 영향 범위는 server-wide (root table). cb-menu 가 사용하는 tmux server 안에서만
 * 적용 — 사용자가 별도 tmux 를 쓰면 영향 없음.
 *
 *   F1     cb-menu session 으로 점프 (= switch-client -t cb-menu)
 *   F2     cb-menu 로 점프 (사용자가 거기서 n 눌러 new)
 *   F3     이전 세션 (switch-client -p)
 *   F4     다음 세션 (switch-client -n)
 *
 * 이전엔 F1/F2 를 popup (cb-helper) 로 바인딩했으나, popup 의 readline UI 가
 * cb-menu 의 ink TUI 보다 단조로워서 일관성을 위해 통합. cb-menu 에는 active
 * 세션이 ▶ 로 표시되므로 Enter 한 번이면 바로 복귀 — popup 의 "임시성" 가치도
 * 거의 잃지 않는다.
 *
 * F1~F4 가 macOS 시스템 키와 겹쳐 사용자가 시스템 설정에서 "Use F1, F2 etc. keys
 * as standard function keys" 활성화하거나 fn+F1 눌러야 동작 — 이 부분은 사용자
 * 환경 책임.
 *
 * F11 (detach) 는 macOS Show Desktop 과 충돌해서 제외. 메뉴에서 q 또는
 * Ctrl-b d 로 detach.
 */

import { spawnSync } from "node:child_process";

function tmux(args: string[]): { code: number; stdout: string; stderr: string } {
  const r = spawnSync("tmux", args, { encoding: "utf-8" });
  return {
    code: r.status ?? -1,
    stdout: r.stdout ?? "",
    stderr: r.stderr ?? "",
  };
}

/** 자기가 도는 tmux session 이름 — cb-menu 또는 cb-<inst>-menu. */
function selfSessionName(): string {
  const r = tmux(["display", "-p", "#S"]);
  const name = r.stdout.trim();
  return name || "cb-menu";
}

export function setupTmuxKeybindings(): void {
  const menuName = selfSessionName();
  const menuTarget = `=${menuName}`;

  const bindings: Array<[string, string[]]> = [
    ["F1", ["switch-client", "-t", menuTarget]],
    ["F2", ["switch-client", "-t", menuTarget]],
    ["F3", ["switch-client", "-p"]],
    ["F4", ["switch-client", "-n"]],
  ];

  for (const [key, action] of bindings) {
    const r = tmux(["bind-key", "-T", "root", key, ...action]);
    if (r.code !== 0) {
      // 실패해도 메뉴는 정상 동작 — Ctrl-b 기반 키바인딩 fallback.
      console.error(`(warn) bind-key ${key} failed: ${r.stderr.trim()}`);
    }
  }
}
