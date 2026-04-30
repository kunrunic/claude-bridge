/**
 * cb-menu TUI ↔ dispatcher RPC helper.
 *
 * 기존 picker.ts 의 rpc() 와 같은 구조 — 별도 파일로 분리해 둔 이유는 menu 가
 * 별도 진입점으로 영속 도는 프로세스라 picker.ts 의 readline 의존성을 끌어들이지
 * 않으려는 것.
 */

import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import {
  connectClient,
  DEFAULT_SOCKET_PATH,
  type CliRequest,
  type CliResponse,
} from "../../../core/ipc.ts";

const SOCKET_PATH = process.env.CB_DISPATCHER_SOCKET ?? DEFAULT_SOCKET_PATH;
const RPC_TIMEOUT_MS = 10_000;

type WithoutRequestId<T> = T extends { request_id: string } ? Omit<T, "request_id"> : never;
type CliRequestArgs = WithoutRequestId<CliRequest>;

export async function rpc(req: CliRequestArgs): Promise<CliResponse> {
  if (!existsSync(SOCKET_PATH)) {
    throw new Error(`dispatcher socket missing: ${SOCKET_PATH}`);
  }
  const ls = await connectClient(SOCKET_PATH);
  const requestId = randomUUID();
  return new Promise<CliResponse>((resolve, reject) => {
    const timer = setTimeout(() => {
      ls.close();
      reject(new Error("RPC timeout"));
    }, RPC_TIMEOUT_MS);
    ls.onMessage((msg) => {
      if (msg.op !== "cli_response") return;
      if (msg.request_id !== requestId) return;
      clearTimeout(timer);
      ls.close();
      resolve(msg);
    });
    ls.send({ ...req, request_id: requestId } as CliRequest);
  });
}
