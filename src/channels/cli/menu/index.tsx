#!/usr/bin/env bun
/**
 * cb-menu 진입점. dispatcher 의 CbMenuSupervisor 가 영속 tmux session 안에
 * 이 프로세스를 띄운다. 사용자는 ssh 진입 시 cb-menu session 에 직접 attach.
 *
 * 환경변수:
 *   CB_DISPATCHER_SOCKET   dispatcher Unix 소켓 (supervisor 가 셋업)
 *   CB_MENU=1              supervisor 가 띄운 표식 (디버깅용, 코드는 안 쓰지만 ps 로 확인 가능)
 */

import { render } from "ink";
import { App } from "./App.tsx";
import { setupTmuxKeybindings } from "./keybindings.ts";

// 메뉴가 도는 시점에 tmux server 가 떠 있는 게 보장되므로 키바인딩 셋업 OK.
// 멱등 — bind-key 는 같은 키에 다시 호출하면 덮어쓰기.
setupTmuxKeybindings();

// supervisor 가 죽으면 자동 재spawn 하므로 unhandled error 도 그냥 throw — 프로세스 종료 → 재기동.
const { waitUntilExit } = render(<App />);
waitUntilExit().catch((err) => {
  console.error("cb-menu crashed:", err);
  process.exit(1);
});
