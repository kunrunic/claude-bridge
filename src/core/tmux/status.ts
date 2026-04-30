/**
 * tmux status bar 조작 헬퍼.
 *
 * session.ts 와 분리한 이유: session lifecycle (new / kill / capture / send-keys)
 * 과 status bar UI 는 다른 책임. 한 파일이 양쪽 다 갖고 있으면 SRP 위반.
 *
 * 모든 호출은 spawnSync (블록킹) — tmux set-option 은 매우 빠르고 (μs 단위),
 * 갱신 주기가 낮아서 부담 없음. 실패 시 anomaly.log 만 남기고 throw 하지 않음
 * (Channel 에러 계약과 정렬).
 */

import { spawnSync } from "node:child_process";
import * as anomaly from "../anomaly.ts";

const TMUX = "tmux";

function run(args: string[]): { code: number; stderr: string } {
  const res = spawnSync(TMUX, args, { encoding: "utf-8" });
  return { code: res.status ?? -1, stderr: res.stderr ?? "" };
}

/**
 * tmux 3.5a 검증 결과: `set-option -t =name ...` 은 "no such session: =name" 으로
 * 실패한다. 다른 명령 (capture-pane -t name:, has-session -t name) 도 `=` prefix
 * 없이 동작하므로 모든 호출에서 prefix 제거.
 *
 * 안전성: cb-s1..cb-s9 형태로 max 9 세션이라 prefix match 충돌 없음 (cb-s1 이
 * cb-s10 에도 매칭되는 경우 — max 9 면 발생 X).
 */
function target(name: string): string {
  return name;
}

/**
 * tmux status-right 텍스트 설정.
 * tmux format string 의 '#' 는 변수 시작 문자라 '##' 로 escape 해야 리터럴로 표시된다.
 */
export function setStatusRight(name: string, text: string): void {
  const escaped = text.replace(/#/g, "##");
  const res = run(["set-option", "-t", target(name), "status-right", escaped]);
  if (res.code !== 0) {
    anomaly.log("tmux_capture_failed", {
      op: "set-status-right",
      name,
      stderr: res.stderr,
    });
  }
}

/** status-right 출력 가능한 최대 길이. tmux 기본값 40 — 미니맵엔 부족. */
export function setStatusRightLength(name: string, length: number): void {
  const res = run([
    "set-option",
    "-t",
    target(name),
    "status-right-length",
    String(length),
  ]);
  if (res.code !== 0) {
    anomaly.log("tmux_capture_failed", {
      op: "set-status-right-length",
      name,
      stderr: res.stderr,
    });
  }
}

/** status bar 표시 on. 일부 환경/테마에서 default off 일 수 있어 명시적으로 설정. */
export function enableStatus(name: string): void {
  const res = run(["set-option", "-t", target(name), "status", "on"]);
  if (res.code !== 0) {
    anomaly.log("tmux_capture_failed", {
      op: "set-status-on",
      name,
      stderr: res.stderr,
    });
  }
}

/**
 * status bar 위치 (top | bottom).
 * cb 세션은 top 으로 두어야 사용자가 attach 했을 때 화면 상단 우측에 minimap 노출.
 * Claude TUI 가 자체 status bar 를 하단에 그리므로 충돌 회피 효과도 있음.
 */
export function setStatusPosition(name: string, position: "top" | "bottom"): void {
  const res = run(["set-option", "-t", target(name), "status-position", position]);
  if (res.code !== 0) {
    anomaly.log("tmux_capture_failed", {
      op: "set-status-position",
      name,
      stderr: res.stderr,
    });
  }
}

/**
 * status bar 줄 수 (1~5).
 * 2 로 두면 status-format[0] (default — minimap 포함) + status-format[1] (shortcuts) 가 각 줄에 표시.
 */
export function setStatusLines(name: string, lines: number): void {
  const res = run(["set-option", "-t", target(name), "status", String(lines)]);
  if (res.code !== 0) {
    anomaly.log("tmux_capture_failed", {
      op: "set-status-lines",
      name,
      stderr: res.stderr,
    });
  }
}

/**
 * status-format[index] 직접 지정.
 * tmux format string 문법 — `#[align=right]` 같은 attribute 사용 가능.
 * `#` 자체를 리터럴로 출력하려면 `##` 로 escape.
 */
export function setStatusFormat(name: string, index: number, format: string): void {
  const escaped = format.replace(/(?<!#)#(?![#A-Za-z\[\{])/g, "##");
  const res = run([
    "set-option",
    "-t",
    target(name),
    `status-format[${index}]`,
    escaped,
  ]);
  if (res.code !== 0) {
    anomaly.log("tmux_capture_failed", {
      op: "set-status-format",
      name,
      stderr: res.stderr,
    });
  }
}

/**
 * status bar 전체 style (bg/fg/attribute).
 * tmux default 는 `bg=green,fg=black` — minimap 색이 background 와 너무 비슷해
 * 안 보임. 검은 바탕 흰 글씨로 통일.
 */
export function setStatusStyle(name: string, style: string): void {
  const res = run(["set-option", "-t", target(name), "status-style", style]);
  if (res.code !== 0) {
    anomaly.log("tmux_capture_failed", {
      op: "set-status-style",
      name,
      stderr: res.stderr,
    });
  }
}

/** generic set-option helper — 위에 정의 안 된 옵션을 직접 설정. */
export function setOption(name: string, key: string, value: string): void {
  const res = run(["set-option", "-t", target(name), key, value]);
  if (res.code !== 0) {
    anomaly.log("tmux_capture_failed", {
      op: `set-option-${key}`,
      name,
      stderr: res.stderr,
    });
  }
}
