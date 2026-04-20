import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { mkdtempSync, rmSync } from "node:fs";
import {
  startServer,
  connectClient,
  type IpcMessage,
} from "../src/core/ipc.ts";

describe("ipc round-trip", () => {
  test("client → server receives hello", async () => {
    const dir = mkdtempSync(join(tmpdir(), "cb-ipc-"));
    const socketPath = join(dir, "test.sock");
    const received: IpcMessage[] = [];

    const server = startServer(socketPath, (ls) => {
      ls.onMessage((m) => received.push(m));
    });

    const client = await connectClient(socketPath);
    client.send({ op: "hello", session_id: "s1", pid: 123 });
    client.send({
      op: "permission_request",
      session_id: "s1",
      request_id: "abcde",
      tool_name: "Bash",
      description: "run",
      input_preview: "{}",
    });

    await new Promise((r) => setTimeout(r, 100));

    expect(received.length).toBe(2);
    expect(received[0]?.op).toBe("hello");
    expect(received[1]?.op).toBe("permission_request");

    client.close();
    server.close();
    rmSync(dir, { recursive: true, force: true });
  });

  test("server → client reply delivered", async () => {
    const dir = mkdtempSync(join(tmpdir(), "cb-ipc-"));
    const socketPath = join(dir, "test.sock");
    const clientReceived: IpcMessage[] = [];

    const server = startServer(socketPath, (ls) => {
      ls.onMessage((m) => {
        if (m.op === "hello") {
          ls.send({ op: "permission_reply", request_id: "abcde", behavior: "allow" });
        }
      });
    });

    const client = await connectClient(socketPath);
    client.onMessage((m) => clientReceived.push(m));
    client.send({ op: "hello", session_id: "s1", pid: 1 });

    await new Promise((r) => setTimeout(r, 100));

    expect(clientReceived.length).toBe(1);
    expect(clientReceived[0]?.op).toBe("permission_reply");

    client.close();
    server.close();
    rmSync(dir, { recursive: true, force: true });
  });
});
