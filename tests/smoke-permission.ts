import { spawn } from "node:child_process";

const proc = spawn("bun", ["run", "src/channels/telegram/server.ts"], {
  cwd: new URL("..", import.meta.url).pathname,
  stdio: ["pipe", "pipe", "pipe"],
  env: { ...process.env, CB_POLL_DISABLED: "1" },
});

proc.stderr.on("data", (d) => process.stderr.write(`[server stderr] ${d}`));

const pending = new Map<number | string, (msg: unknown) => void>();
let buf = "";
proc.stdout.on("data", (chunk: Buffer) => {
  buf += chunk.toString("utf-8");
  for (;;) {
    const nl = buf.indexOf("\n");
    if (nl < 0) break;
    const line = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    if (!line) continue;
    let msg: { id?: number | string };
    try {
      msg = JSON.parse(line);
    } catch {
      continue;
    }
    if (msg.id !== undefined && pending.has(msg.id)) {
      pending.get(msg.id)!(msg);
      pending.delete(msg.id);
    }
  }
});

function request<T>(method: string, params: unknown, id: number): Promise<T> {
  const payload = { jsonrpc: "2.0", id, method, params };
  return new Promise<T>((resolve, reject) => {
    pending.set(id, (msg) => resolve(msg as T));
    proc.stdin.write(JSON.stringify(payload) + "\n");
    setTimeout(() => {
      if (pending.has(id)) reject(new Error(`timeout ${method}`));
    }, 10000);
  });
}

function notify(method: string, params: unknown): void {
  const payload = { jsonrpc: "2.0", method, params };
  proc.stdin.write(JSON.stringify(payload) + "\n");
}

try {
  await request("initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "smoke-perm", version: "0.1.0" },
  }, 1);
  notify("notifications/initialized", {});

  notify("notifications/claude/channel/permission_request", {
    request_id: "abcde",
    tool_name: "Bash",
    description: "Run git status",
    input_preview: JSON.stringify({ command: "git status" }),
  });

  await new Promise((r) => setTimeout(r, 1500));
  console.log("permission_request notification dispatched — check Telegram for 🔐 prompt");
} finally {
  proc.stdin.end();
  proc.kill();
}
