/**
 * hosts.ts — cb 클라이언트 호스트 유틸.
 *
 * isLocalHost: 등록 호스트가 로컬 머신을 가리키는지 판정 (localhost/127.0.0.1/::1).
 * 로컬이면 cb 가 ssh 를 건너뛰고 직접 tmux attach 하므로, 이 판정이 정확해야 한다.
 */

import { describe, expect, test } from "bun:test";
import { isLocalHost, type HostEntry } from "../src/channels/cli/hosts.ts";

function entry(host: string): HostEntry {
  return { host };
}

describe("isLocalHost", () => {
  test("localhost 는 로컬", () => {
    expect(isLocalHost(entry("localhost"))).toBe(true);
  });

  test("127.0.0.1 은 로컬", () => {
    expect(isLocalHost(entry("127.0.0.1"))).toBe(true);
  });

  test("IPv6 루프백 ::1 은 로컬", () => {
    expect(isLocalHost(entry("::1"))).toBe(true);
  });

  test("대소문자 무시 — LOCALHOST 도 로컬", () => {
    expect(isLocalHost(entry("LOCALHOST"))).toBe(true);
    expect(isLocalHost(entry("LocalHost"))).toBe(true);
  });

  test("앞뒤 공백 무시", () => {
    expect(isLocalHost(entry("  localhost  "))).toBe(true);
  });

  test("원격 IP 는 로컬 아님", () => {
    expect(isLocalHost(entry("192.168.1.10"))).toBe(false);
  });

  test("도메인은 로컬 아님", () => {
    expect(isLocalHost(entry("example.com"))).toBe(false);
    expect(isLocalHost(entry("home"))).toBe(false);
  });

  test("빈 host 는 로컬 아님", () => {
    expect(isLocalHost(entry(""))).toBe(false);
  });

  test("port/user/key 가 있어도 host 만으로 판정", () => {
    expect(
      isLocalHost({ host: "localhost", port: 2222, user: "me", key: "~/.ssh/id_ed25519" }),
    ).toBe(true);
  });
});
