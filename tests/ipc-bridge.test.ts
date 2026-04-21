import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { mkdtempSync, rmSync } from "node:fs";
import { startServer, type IpcMessage } from "../src/core/ipc.ts";
import { IpcBridge } from "../src/channels/telegram/IpcBridge.ts";

function tmpSocket(): { dir: string; path: string } {
  const dir = mkdtempSync(join(tmpdir(), "cb-ipcbridge-"));
  return { dir, path: join(dir, "test.sock") };
}

describe("IpcBridge", () => {
  test("connect → hello 전송됨", async () => {
    const { dir, path } = tmpSocket();
    const received: IpcMessage[] = [];
    const server = startServer(path, (ls) => ls.onMessage((m) => received.push(m)));

    const bridge = new IpcBridge();
    await bridge.connect(path, "s1", 42, {
      emitInbound: () => {},
      sendPermissionReply: () => {},
    });

    await new Promise((r) => setTimeout(r, 50));
    expect(received[0]?.op).toBe("hello");
    expect((received[0] as { session_id: string }).session_id).toBe("s1");

    bridge.close();
    server.close();
    rmSync(dir, { recursive: true, force: true });
  });

  test("connected: connect 전 false, 후 true", async () => {
    const { dir, path } = tmpSocket();
    const server = startServer(path, () => {});
    const bridge = new IpcBridge();
    expect(bridge.connected).toBe(false);
    await bridge.connect(path, "s1", 1, { emitInbound: () => {}, sendPermissionReply: () => {} });
    expect(bridge.connected).toBe(true);
    bridge.close();
    server.close();
    rmSync(dir, { recursive: true, force: true });
  });

  test("inbound 메시지 → emitInbound 호출", async () => {
    const { dir, path } = tmpSocket();
    let serverSocket: ReturnType<typeof startServer> | undefined;
    const received: unknown[] = [];
    const inboundCalls: Array<{ content: string; meta: Record<string, string> }> = [];

    const server = startServer(path, (ls) => {
      serverSocket = server;
      // 연결 후 서버에서 inbound 전송
      setTimeout(() => {
        ls.send({ op: "inbound", content: "안녕", meta: { chat_id: "c1" } });
      }, 30);
    });

    const bridge = new IpcBridge();
    await bridge.connect(path, "s1", 1, {
      emitInbound: (content, meta) => inboundCalls.push({ content, meta }),
      sendPermissionReply: () => {},
    });

    await new Promise((r) => setTimeout(r, 100));
    expect(inboundCalls.length).toBe(1);
    expect(inboundCalls[0]!.content).toBe("안녕");
    expect(inboundCalls[0]!.meta.chat_id).toBe("c1");

    bridge.close();
    server.close();
    rmSync(dir, { recursive: true, force: true });
  });

  test("permission_reply 메시지 → sendPermissionReply 호출", async () => {
    const { dir, path } = tmpSocket();
    const replyCalls: Array<{ requestId: string; behavior: string }> = [];

    const server = startServer(path, (ls) => {
      setTimeout(() => {
        ls.send({ op: "permission_reply", request_id: "req-1", behavior: "allow" });
      }, 30);
    });

    const bridge = new IpcBridge();
    await bridge.connect(path, "s1", 1, {
      emitInbound: () => {},
      sendPermissionReply: (requestId, behavior) => replyCalls.push({ requestId, behavior }),
    });

    await new Promise((r) => setTimeout(r, 100));
    expect(replyCalls.length).toBe(1);
    expect(replyCalls[0]!.requestId).toBe("req-1");
    expect(replyCalls[0]!.behavior).toBe("allow");

    bridge.close();
    server.close();
    rmSync(dir, { recursive: true, force: true });
  });

  test("send() → 서버에 메시지 전달", async () => {
    const { dir, path } = tmpSocket();
    const serverReceived: IpcMessage[] = [];
    const server = startServer(path, (ls) => ls.onMessage((m) => serverReceived.push(m)));

    const bridge = new IpcBridge();
    await bridge.connect(path, "s1", 1, { emitInbound: () => {}, sendPermissionReply: () => {} });
    await new Promise((r) => setTimeout(r, 30));

    bridge.send({ op: "signal", signal: "busy" } as unknown as Record<string, unknown>);
    await new Promise((r) => setTimeout(r, 50));

    // hello + signal
    expect(serverReceived.length).toBe(2);
    expect(serverReceived[1]?.op).toBe("signal");

    bridge.close();
    server.close();
    rmSync(dir, { recursive: true, force: true });
  });

  test("session_state 수신 → sessionActive/sessionLabel 반영", async () => {
    const { dir, path } = tmpSocket();
    const server = startServer(path, (ls) => {
      setTimeout(() => {
        ls.send({ op: "session_state", session_id: "s1", is_active: false, label: "backend" });
      }, 30);
    });

    const bridge = new IpcBridge();
    expect(bridge.sessionActive).toBe(false);
    expect(bridge.sessionLabel).toBe("");

    await bridge.connect(path, "s1", 1, { emitInbound: () => {}, sendPermissionReply: () => {} });
    await new Promise((r) => setTimeout(r, 100));

    expect(bridge.sessionActive).toBe(false);
    expect(bridge.sessionLabel).toBe("backend");

    bridge.close();
    server.close();
    rmSync(dir, { recursive: true, force: true });
  });

  test("session_state active=true → sessionActive=true", async () => {
    const { dir, path } = tmpSocket();
    const server = startServer(path, (ls) => {
      setTimeout(() => {
        ls.send({ op: "session_state", session_id: "s1", is_active: true, label: "x" });
      }, 30);
    });

    const bridge = new IpcBridge();
    await bridge.connect(path, "s1", 1, { emitInbound: () => {}, sendPermissionReply: () => {} });
    await new Promise((r) => setTimeout(r, 100));

    expect(bridge.sessionActive).toBe(true);
    expect(bridge.sessionLabel).toBe("x");

    bridge.close();
    server.close();
    rmSync(dir, { recursive: true, force: true });
  });

  test("onClose → 콜백 호출", async () => {
    const { dir, path } = tmpSocket();
    let closed = false;
    let serverLs: Parameters<Parameters<typeof startServer>[1]>[0] | undefined;
    const server = startServer(path, (ls) => { serverLs = ls; });

    const bridge = new IpcBridge();
    await bridge.connect(path, "s1", 1, {
      emitInbound: () => {},
      sendPermissionReply: () => {},
      onClose: () => { closed = true; },
    });
    await new Promise((r) => setTimeout(r, 30));

    // 서버 측에서 연결 끊기
    serverLs?.close();
    await new Promise((r) => setTimeout(r, 100));
    expect(closed).toBe(true);

    bridge.close(); // 재연결 시도 중단
    server.close();
    rmSync(dir, { recursive: true, force: true });
  });
});
