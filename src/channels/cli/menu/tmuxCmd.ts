/**
 * cb-menu 안에서 tmux 명령을 실행하는 얇은 wrapper.
 *
 * cb-menu 는 영속 tmux session 안에서 도는 프로세스라 self = current client.
 * switch-client / detach-client 모두 인자 없이 호출하면 self 대상.
 */

import { spawnSync } from "node:child_process";

export type TmuxCmdResult = { code: number; stderr: string };

function tmux(args: string[]): TmuxCmdResult {
  const r = spawnSync("tmux", args, { encoding: "utf-8" });
  return { code: r.status ?? -1, stderr: r.stderr ?? "" };
}

/** self 를 다른 session 으로 점프. tmux name 은 정확히 일치해야 하므로 `=` 접두. */
export function switchToSession(name: string): TmuxCmdResult {
  return tmux(["switch-client", "-t", `=${name}`]);
}

/** disconnect — ssh 접속 해제. 메뉴 프로세스는 그대로 영속. */
export function disconnectSelf(): TmuxCmdResult {
  return tmux(["detach-client"]);
}

export function killTmuxSession(name: string): TmuxCmdResult {
  return tmux(["kill-session", "-t", `=${name}`]);
}
