import { createServer, createConnection, type Socket, type Server as NetServer } from "node:net";
import { dirname } from "node:path";
import { unlinkSync, existsSync, mkdirSync } from "node:fs";
import * as anomaly from "./anomaly.ts";
import { paths } from "./paths.ts";

export const DEFAULT_SOCKET_PATH = paths.socketPath;

export type IpcHello = {
  op: "hello";
  session_id: string;
  pid: number;
};

export type IpcInbound = {
  op: "inbound";
  content: string;
  meta: Record<string, string>;
};

export type IpcPermissionRequest = {
  op: "permission_request";
  session_id: string;
  request_id: string;
  tool_name: string;
  description: string;
  input_preview: string;
};

export type IpcPermissionReply = {
  op: "permission_reply";
  request_id: string;
  behavior: "allow" | "deny";
};

export type IpcSignal = {
  op: "signal";
  session_id: string;
  signal: string;
};

export type IpcShutdown = {
  op: "shutdown";
};

export type IpcReplySent = {
  op: "reply_sent";
  session_id: string;
};

export type IpcMessage =
  | IpcHello
  | IpcInbound
  | IpcPermissionRequest
  | IpcPermissionReply
  | IpcSignal
  | IpcShutdown
  | IpcReplySent;

export class LineSocket {
  private buf = "";

  constructor(public readonly sock: Socket) {}

  send(msg: IpcMessage): void {
    try {
      this.sock.write(JSON.stringify(msg) + "\n");
    } catch (err) {
      anomaly.log("anomaly_self_error", {
        where: "ipc.send",
        op: msg.op,
        error: String(err),
      });
    }
  }

  onMessage(cb: (msg: IpcMessage) => void): void {
    this.sock.on("data", (chunk: Buffer) => {
      this.buf += chunk.toString("utf-8");
      for (;;) {
        const nl = this.buf.indexOf("\n");
        if (nl < 0) break;
        const line = this.buf.slice(0, nl).trim();
        this.buf = this.buf.slice(nl + 1);
        if (!line) continue;
        try {
          cb(JSON.parse(line) as IpcMessage);
        } catch (err) {
          anomaly.log("anomaly_self_error", {
            where: "ipc.parse",
            line: line.slice(0, 100),
            error: String(err),
          });
        }
      }
    });
  }

  onClose(cb: () => void): void {
    this.sock.on("close", cb);
    this.sock.on("error", cb);
  }

  close(): void {
    this.sock.destroy();
  }
}

export function startServer(
  path: string,
  onConnection: (ls: LineSocket) => void,
): NetServer {
  mkdirSync(dirname(path), { recursive: true });
  if (existsSync(path)) {
    try {
      unlinkSync(path);
    } catch {
      // ignore; listen() will fail and be logged
    }
  }
  const server = createServer((sock) => onConnection(new LineSocket(sock)));
  server.listen(path);
  return server;
}

export function connectClient(path: string): Promise<LineSocket> {
  return new Promise((resolve, reject) => {
    const sock = createConnection(path);
    sock.once("connect", () => resolve(new LineSocket(sock)));
    sock.once("error", reject);
  });
}
