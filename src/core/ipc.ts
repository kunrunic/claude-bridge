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
  message_ids?: number[];
};

export type IpcSessionState = {
  op: "session_state";
  session_id: string;
  is_active: boolean;
  label: string;
};

// handoff — 로컬 claude 세션을 bridge tmux 로 이관. dispatcher 가 받아서
// sessions.spawn({ cwd, resumeId }) 를 실행한다. 인증은 유닉스 소켓 권한에 의존
// (같은 사용자 프로세스만 접근 가능).
export type IpcSpawnRequest = {
  op: "spawn_request";
  cwd: string;
  resumeId?: string;
  skipPermissions?: boolean;
};

// ── CLI (cb 명령어) ↔ dispatcher RPC ──────────────────────────────────────
//
// 같은 dispatcher Unix 소켓을 MCP server 와 공유한다. op 로 분기.
// 각 cb 호출 = 1 connection: connect → cli_request → cli_response → close.

export type CliRequest =
  | { op: "cli_request"; request_id: string; command: "list_sessions" }
  | {
      op: "cli_request";
      request_id: string;
      command: "spawn";
      cwd?: string;
      skipPermissions?: boolean;
    }
  | { op: "cli_request"; request_id: string; command: "kill"; target: string }
  | { op: "cli_request"; request_id: string; command: "list_recent"; limit?: number }
  | {
      op: "cli_request";
      request_id: string;
      command: "resume";
      target: string;
      fork?: boolean;
      skipPermissions?: boolean;
    };

export type CliSessionInfo = {
  id: string;
  label: string;
  tmuxName: string;
  state: string;
  signal: string;
  isActive: boolean;
};

export type CliRecentInfo = {
  id: string;
  project: string;
  title: string;
  mtimeText: string;
};

export type CliResponseData =
  | { kind: "list_sessions"; sessions: CliSessionInfo[]; activeId?: string }
  // tmuxName 포함 — cb new 가 spawn 응답 받자마자 추가 RPC 없이 바로 tmux attach 가능.
  | { kind: "spawn"; id: string; label: string; tmuxName: string }
  | { kind: "kill"; success: boolean; message?: string }
  | { kind: "list_recent"; recent: CliRecentInfo[] }
  | { kind: "resume"; message: string };

export type CliResponse = {
  op: "cli_response";
  request_id: string;
  ok: boolean;
  data?: CliResponseData;
  error?: string;
};

export type IpcMessage =
  | IpcHello
  | IpcInbound
  | IpcPermissionRequest
  | IpcPermissionReply
  | IpcSignal
  | IpcShutdown
  | IpcReplySent
  | IpcSessionState
  | IpcSpawnRequest
  | CliRequest
  | CliResponse;

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
