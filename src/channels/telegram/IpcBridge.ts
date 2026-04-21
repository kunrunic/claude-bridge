import { connectClient, type LineSocket } from "../../core/ipc.ts";
import * as anomaly from "../../core/anomaly.ts";

const RECONNECT_DELAYS_MS = [2_000, 5_000, 10_000];

export type IpcBridgeDeps = {
  emitInbound: (content: string, meta: Record<string, string>) => void;
  sendPermissionReply: (requestId: string, behavior: "allow" | "deny") => void;
  onClose?: () => void;
  onReconnecting?: (attempt: number, maxAttempts: number) => void;
  onReconnected?: () => void;
  onReconnectFailed?: () => void;
};

export class IpcBridge {
  private socket: LineSocket | undefined;
  private closed = false;
  private socketPath = "";
  private sessionId = "";
  private pid = 0;
  private deps: IpcBridgeDeps | undefined;
  private sessionState: { is_active: boolean; label: string } | undefined;

  get sessionActive(): boolean {
    return this.sessionState?.is_active ?? false;
  }

  get sessionLabel(): string {
    return this.sessionState?.label ?? "";
  }

  async connect(socketPath: string, sessionId: string, pid: number, deps: IpcBridgeDeps): Promise<void> {
    this.socketPath = socketPath;
    this.sessionId = sessionId;
    this.pid = pid;
    this.deps = deps;
    anomaly.log("ipc_connect_start", {
      where: "IpcBridge.connect",
      sessionId,
      socketPath,
      pid,
    });
    try {
      await this.attach();
      anomaly.log("ipc_connect_ok", {
        where: "IpcBridge.connect",
        sessionId,
      });
    } catch (err) {
      anomaly.log("ipc_connect_failed", {
        where: "IpcBridge.connect",
        sessionId,
        socketPath,
        error: String(err),
      });
      throw err;
    }
  }

  private async attach(): Promise<void> {
    this.socket = await connectClient(this.socketPath);
    this.socket.send({ op: "hello", session_id: this.sessionId, pid: this.pid });

    this.socket.onMessage((msg) => {
      switch (msg.op) {
        case "inbound":
          this.deps?.emitInbound(msg.content, msg.meta);
          break;
        case "permission_reply":
          this.deps?.sendPermissionReply(msg.request_id, msg.behavior);
          break;
        case "session_state":
          this.sessionState = {
            is_active: msg.is_active,
            label: msg.label,
          };
          break;
        default:
          anomaly.log("mcp_unknown_method", {
            where: "IpcBridge.onMessage",
            op: msg.op,
          });
      }
    });

    this.socket.onClose(() => {
      if (this.closed) return;
      this.deps?.onClose?.();
      anomaly.log("anomaly_self_error", {
        where: "IpcBridge.onClose",
        sessionId: this.sessionId,
      });
      void this.reconnect();
    });
  }

  private async reconnect(): Promise<void> {
    for (let i = 0; i < RECONNECT_DELAYS_MS.length; i++) {
      this.deps?.onReconnecting?.(i + 1, RECONNECT_DELAYS_MS.length);
      await new Promise((r) => setTimeout(r, RECONNECT_DELAYS_MS[i]));
      if (this.closed) return;
      try {
        await this.attach();
        this.deps?.onReconnected?.();
        return;
      } catch {
        // retry
      }
    }
    this.deps?.onReconnectFailed?.();
    anomaly.log("anomaly_self_error", {
      where: "IpcBridge.reconnect",
      reason: "max_retries_exhausted",
      sessionId: this.sessionId,
    });
  }

  send(msg: Record<string, unknown>): void {
    if (this.socket) {
      this.socket.send(msg as Parameters<typeof this.socket.send>[0]);
    }
  }

  close(): void {
    this.closed = true;
    if (this.socket) {
      this.socket.close();
    }
  }

  get connected(): boolean {
    return this.socket !== undefined && !this.closed;
  }
}
