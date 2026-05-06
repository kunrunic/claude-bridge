/**
 * cb-menu 가 mount 되기 직전 1회 셋업하는 tmux 키바인딩.
 *
 * 영향 범위는 server-wide (root table). cb-menu 가 사용하는 tmux server 안에서만
 * 적용 — 사용자가 별도 tmux 를 쓰면 영향 없음.
 *
 *   F1     cb-menu (sessions 목록)
 *   F2     cb-menu → new session 모드 자동 진입
 *   F3     이전 세션 (switch-client -p)
 *   F4     다음 세션 (switch-client -n)
 *   F5     leave session — cb-menu 로 복귀 (claude 는 그대로 background 실행)
 *   F6     handoff to bridge — 현재 Claude 세션을 Telegram active 로 전환 후 cb-menu 로 복귀
 *
 * SSH 자체를 끊으려면 cb-menu 안에서 `q` 를 누른다 (detach-client).
 *
 * F1~F6 가 macOS 시스템 키와 겹쳐 사용자가 시스템 설정에서 "Use F1, F2 etc. keys
 * as standard function keys" 활성화하거나 fn+Fx 눌러야 동작.
 */

import { spawnSync } from "node:child_process";
import { DEFAULT_SOCKET_PATH } from "../../../core/ipc.ts";

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
  const socket = process.env.CB_DISPATCHER_SOCKET ?? DEFAULT_SOCKET_PATH;

  // Shift+Enter 등 extended key sequence — start.sh 에서도 시도하지만
  // dispatcher 시작 시점에 tmux 서버가 아직 안 떠 있으면 silent fail 한다.
  // 이 함수는 cb-menu (= tmux 서버 확실히 살아있을 때) 안에서 실행되므로
  // 여기서 다시 셋업하면 항상 적용됨.
  // client-attached 훅: 매 attach 마다 refresh-client 로 extended key 재협상.
  tmux(["set", "-g", "extended-keys", "on"]);
  tmux(["set-hook", "-g", "client-attached", "refresh-client"]);

  // scrollback / copy-mode UX. mouse on 은 attach 시점에 cli.ts 가 TERM_PROGRAM 보고
  // 분기 set 하므로 여기엔 없음. 나머지는 server-wide 이라 한 번 set 하면 모든 세션 적용.
  // smcup@:rmcup@ — alt-screen 끔. 터미널 main buffer 에 tmux 출력이 누적되어
  // iTerm/Terminal.app/VSCode 의 native scrollback 으로 모든 history 스크롤 가능.
  tmux(["set", "-g", "history-limit", "50000"]);
  tmux(["set", "-as", "terminal-features", "*:extkeys"]);
  tmux(["set", "-ga", "terminal-overrides", "*:smcup@:rmcup@"]);
  tmux(["set", "-wg", "mode-keys", "vi"]);

  // F6: 현재 세션의 CB_SESSION_ID 로 set_active_request 전송 후 cb-menu 로 복귀
  const f6Cmd = [
    `SESS=$(tmux show-environment -t '#S' CB_SESSION_ID 2>/dev/null | cut -d= -f2)`,
    `[ -z "$SESS" ] && exit 0`,
    `printf '{"op":"set_active_request","session_id":"%s"}\\n' "$SESS" | nc -U -w 1 '${socket}' >/dev/null 2>&1`,
    `tmux switch-client -t '${menuTarget}'`,
  ].join("; ");

  // F2: switch-client + send-keys 를 단일 문자열로 묶어 bind-key 의 bound command 로 전달.
  // 분리된 argv 로 ';' 를 넘기면 tmux 가 OUTER 분리자로 해석해 두번째 커맨드(run-shell 등)가
  // bind 시점에 즉시 실행됐다. 단일 문자열의 ';' 는 bind 내부의 command list 분리자로 해석됨.
  // send-keys 는 세션 이름으로 직접 타게팅하므로 sleep 불필요.
  const f2Cmd = `switch-client -t '${menuTarget}' ; send-keys -t '${menuTarget}' n`;

  const bindings: Array<[string, string[]]> = [
    ["F1", ["switch-client", "-t", menuTarget]],
    ["F2", [f2Cmd]],
    ["F3", ["switch-client", "-p"]],
    ["F4", ["switch-client", "-n"]],
    ["F5", ["switch-client", "-t", menuTarget]],
    ["F6", ["run-shell", f6Cmd]],
  ];

  for (const [key, action] of bindings) {
    const r = tmux(["bind-key", "-T", "root", key, ...action]);
    if (r.code !== 0) {
      console.error(`(warn) bind-key ${key} failed: ${r.stderr.trim()}`);
    }
  }
}
