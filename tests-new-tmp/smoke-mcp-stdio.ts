import { spawn } from "node:child_process";
import { loadConfig } from "../src/config.ts";

const cfg = loadConfig();
if (!cfg.defaultChatId) throw new Error("defaultChatId missing");

const proc = spawn("bun", ["run", "src/server.ts"], {
  cwd: new URL("..", import.meta.url).pathname,
  stdio: ["pipe", "pipe", "pipe"],
  env: { ...process.env, CB_POLL_DISABLED: "1" },
});

proc.stderr.on("data", (d) => process.stderr.write(`[server stderr] ${d}`));

let buf = "";
const pending = new Map<number | string, (msg: unknown) => void>();

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
      console.error("[non-json]", line);
      continue;
    }
    if (msg.id !== undefined && pending.has(msg.id)) {
      pending.get(msg.id)!(msg);
      pending.delete(msg.id);
    } else {
      console.log("[notify]", line);
    }
  }
});

function request<T>(method: string, params: unknown, id: number): Promise<T> {
  const payload = { jsonrpc: "2.0", id, method, params };
  return new Promise<T>((resolve, reject) => {
    pending.set(id, (msg: unknown) => resolve(msg as T));
    proc.stdin.write(JSON.stringify(payload) + "\n");
    setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        reject(new Error(`timeout on ${method}`));
      }
    }, 10000);
  });
}

function notify(method: string, params: unknown): void {
  const payload = { jsonrpc: "2.0", method, params };
  proc.stdin.write(JSON.stringify(payload) + "\n");
}

try {
  const init = await request<{ result: { serverInfo: unknown } }>(
    "initialize",
    {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "smoke-test", version: "0.1.0" },
    },
    1,
  );
  console.log("initialize OK:", JSON.stringify(init.result.serverInfo));
  notify("notifications/initialized", {});

  const tools = await request<{ result: { tools: { name: string }[] } }>(
    "tools/list",
    {},
    2,
  );
  console.log(
    "tools/list OK:",
    tools.result.tools.map((t) => t.name).join(", "),
  );

  const reply = await request<{
    result: { content: { type: string; text: string }[]; isError?: boolean };
  }>(
    "tools/call",
    {
      name: "reply",
      arguments: {
        chat_id: cfg.defaultChatId,
        text: "[stage1 mcp stdio smoke] end-to-end OK",
      },
    },
    3,
  );

  if (reply.result.isError) {
    console.error("reply FAILED:", JSON.stringify(reply.result));
    process.exitCode = 1;
  } else {
    console.log("reply OK:", reply.result.content[0]?.text);
  }
} finally {
  proc.stdin.end();
  proc.kill();
}
