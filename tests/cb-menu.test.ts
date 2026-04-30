/**
 * CbMenuSupervisor — tmux session 영속 관리.
 *
 * 실제 tmux 서버 위에서 동작 (CI 에 tmux 가 있다는 전제). 다음을 검증:
 *  - start() → tmux session 생성
 *  - 사용자가 session 죽임 → tick() 호출 시 재생성 (watchdog 본질)
 *  - stop() → session 정리 + 폴링 중단
 */

import { describe, expect, test, afterEach } from "bun:test";
import { CbMenuSupervisor } from "../src/core/cbMenu.ts";
import * as tmux from "../src/core/tmux/session.ts";

const TEST_NAME = `cb-menu-test-${process.pid}`;
const NOOP_ENTRY = "/dev/null";  // bun run /dev/null → 즉시 종료. 그래도 newSession 자체는 성공.

afterEach(() => {
  try {
    tmux.killSession(TEST_NAME);
  } catch {}
});

describe("CbMenuSupervisor", () => {
  test("start 시 tmux session 생성", async () => {
    const sup = new CbMenuSupervisor({
      tmuxName: TEST_NAME,
      // 영속적으로 도는 명령 (sleep 무한). bun run 대신 단순 sh 명령.
      // CbMenuSupervisor 가 `bun run <entry>` 로 실행하는데, entry 가 sh 스크립트면 bun 이 sh 라고 인식 못 함.
      // 우회: sh 호환 .sh 만들기보다 entry 를 직접 sh 명령으로 대체할 수 있어야.
      // 현재 cbMenu.ts 는 entry 만 인자로 받고 `bun run` prefix 가 hardcoded.
      // 따라서 이 테스트에선 supervisor 의 respawn 로직 동작만 보고, 실제 entry 명령은 bun 이 해석할 수 있는 것으로.
      entry: NOOP_ENTRY,
      socketPath: "/tmp/test-not-real.sock",
    });
    sup.start();
    // tmux new-session 은 동기적 — start() 직후 has-session 이 true 여야 함.
    // 하지만 bun run /dev/null 이 즉시 종료해서 session 도 즉시 죽을 수 있음.
    // 여기선 한 번이라도 session 이 생성됐는지만 본다.
    let sawSession = false;
    for (let i = 0; i < 30; i++) {
      if (tmux.hasSession(TEST_NAME)) {
        sawSession = true;
        break;
      }
      await new Promise((r) => setTimeout(r, 50));
    }
    sup.stop();
    expect(sawSession).toBe(true);
  });

  test("stop 후 tmux session 제거 + 폴링 중단", async () => {
    const sup = new CbMenuSupervisor({
      tmuxName: TEST_NAME,
      entry: NOOP_ENTRY,
      socketPath: "/tmp/test-not-real.sock",
    });
    sup.start();
    // 잠깐 기다려 session 생성 확인 후 stop.
    await new Promise((r) => setTimeout(r, 200));
    sup.stop();
    // stop 후 session 없어야.
    expect(tmux.hasSession(TEST_NAME)).toBe(false);
    // 추가로 1초 더 기다려서 polling 이 재spawn 하지 않는지 확인.
    await new Promise((r) => setTimeout(r, 1200));
    expect(tmux.hasSession(TEST_NAME)).toBe(false);
  });

  test("tick() 가 죽은 session 을 재spawn", async () => {
    const sup = new CbMenuSupervisor({
      tmuxName: TEST_NAME,
      entry: NOOP_ENTRY,
      socketPath: "/tmp/test-not-real.sock",
    });
    sup.start();
    // 외부에서 session kill — entry 가 즉시 종료해서 이미 없을 수도 있음.
    try {
      tmux.killSession(TEST_NAME);
    } catch {}
    expect(tmux.hasSession(TEST_NAME)).toBe(false);
    // tick 으로 강제 점검 → 재spawn.
    sup.tick();
    let revived = false;
    for (let i = 0; i < 30; i++) {
      if (tmux.hasSession(TEST_NAME)) {
        revived = true;
        break;
      }
      await new Promise((r) => setTimeout(r, 50));
    }
    sup.stop();
    expect(revived).toBe(true);
  });
});
