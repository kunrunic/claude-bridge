/**
 * cb-menu 최상위 컴포넌트.
 *
 *   - 'sessions' 모드: SessionList. attach / kill / detach / 모드 전환
 *   - 'new' 모드: DirBrowser. cwd 확정 → 'permission' 모드로 전이
 *   - 'resume' 모드: ResumePicker. 선택 → 'permission' 모드로 전이
 *   - 'permission' 모드: PermissionPrompt. y/N → spawn 또는 resume RPC 실행
 *                       (resume 응답엔 tmuxName 없음 — 폴링이 픽업, 사용자가 직접 attach)
 *
 * list_sessions 는 1.5초 polling. SessionEvent 구독 흐름 도입은 별도 작업.
 */

import { Box, Text, useStdout } from "ink";
import { useEffect, useState } from "react";
import { rpc } from "./rpc.ts";
import { switchToSession, disconnectSelf, killTmuxSession } from "./tmuxCmd.ts";
import { SessionList } from "./SessionList.tsx";
import { DirBrowser } from "./DirBrowser.tsx";
import { ResumePicker } from "./ResumePicker.tsx";
import { PermissionPrompt } from "./PermissionPrompt.tsx";
import { useSpinnerFrame } from "./spinner.ts";
import type { CliSessionInfo } from "../../../core/ipc.ts";

type Mode = "sessions" | "new" | "resume" | "permission";

type Pending =
  | { kind: "new"; cwd: string }
  | { kind: "resume"; id: string };

const POLL_MS = 1500;

function formatMinimap(sessions: CliSessionInfo[], spin: string): string {
  const sorted = [...sessions]
    .filter((s) => s.state !== "dead")
    .sort((a, b) => a.id.localeCompare(b.id));
  if (sorted.length === 0) return "(no sessions)";
  const visible = sorted.slice(0, 6);
  const overflow = sorted.length - visible.length;
  const parts = visible.map((s) => {
    const prefix = s.isActive ? "▶" : " ";
    const tag = s.signal === "busy" || s.state === "spawning" ? spin : "·";
    const label = s.label.length > 8 ? s.label.slice(0, 7) + "…" : s.label;
    return `${prefix}${s.id}${tag}${label}`;
  });
  if (overflow > 0) parts.push(`+${overflow}`);
  return parts.join("  ");
}

export function App() {
  const [mode, setMode] = useState<Mode>("sessions");
  const [sessions, setSessions] = useState<CliSessionInfo[]>([]);
  const [pending, setPending] = useState<Pending | undefined>(undefined);
  const [error, setError] = useState<string>();
  const [info, setInfo] = useState<string>();
  const spin = useSpinnerFrame();
  const { stdout } = useStdout();
  const termRows = stdout?.rows ?? 24;

  // 세션 목록 polling.
  useEffect(() => {
    let alive = true;
    const refresh = async (): Promise<void> => {
      try {
        const r = await rpc({ op: "cli_request", command: "list_sessions" });
        if (!alive) return;
        if (r.ok && r.data?.kind === "list_sessions") {
          setSessions(r.data.sessions);
          setError(undefined);
        } else if (!r.ok) {
          setError(r.error ?? "list_sessions failed");
        }
      } catch (e) {
        if (alive) setError(String(e));
      }
    };
    void refresh();
    const t = setInterval(() => void refresh(), POLL_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  // info 메시지 자동 사라짐 (3초).
  useEffect(() => {
    if (!info) return;
    const t = setTimeout(() => setInfo(undefined), 3000);
    return () => clearTimeout(t);
  }, [info]);

  const onSelect = (s: CliSessionInfo): void => {
    // cb-menu 에서 active 세션을 선택 = SSH 가 control 을 가져옴.
    // origin 무관(Telegram-spawn 도 동일) — Telegram active 해제하고 [SSH] 로 전환.
    // 다시 Telegram 으로 넘기려면 그 세션 안에서 F6.
    if (s.isActive) {
      void rpc({
        op: "cli_request",
        command: "clear_active",
        expected_session_id: s.id,
      }).catch((e) => setError(`clear_active 실패: ${String(e)}`));
    }
    const r = switchToSession(s.tmuxName);
    if (r.code !== 0) setError(`switch-client 실패: ${r.stderr.trim()}`);
  };

  const doSpawn = async (cwd: string, skipPermissions: boolean): Promise<void> => {
    setInfo(`▶ spawning at ${cwd}${skipPermissions ? " (skip-perm)" : ""}...`);
    try {
      const r = await rpc({ op: "cli_request", command: "spawn", cwd, skipPermissions });
      if (r.ok && r.data?.kind === "spawn") {
        setInfo(`✓ spawned [${r.data.id}][${r.data.label}] — attaching`);
        // dispatcher 가 새 tmux session 만들 때까지 잠깐 기다림. supervise 가 끝났으면
        // tmux switch-client 가 즉시 OK. spawn 직후 has-session 가 안 보일 수 있어
        // 짧은 retry.
        const tmuxName = r.data.tmuxName;
        await waitForTmuxAndSwitch(tmuxName, (m) => setError(m));
        setMode("sessions");
      } else {
        setError(r.error ?? "spawn failed");
      }
    } catch (e) {
      setError(String(e));
    }
  };

  const doResume = async (id: string, skipPermissions: boolean): Promise<void> => {
    setInfo(`▶ resuming ${id}${skipPermissions ? " (skip-perm)" : ""}...`);
    try {
      const r = await rpc({ op: "cli_request", command: "resume", target: id, skipPermissions });
      if (r.ok && r.data?.kind === "resume") {
        setInfo(r.data.message);
        // resume 응답엔 tmuxName 없음. polling 으로 새 세션이 등록되면 사용자가 직접 attach.
        setMode("sessions");
      } else {
        setError(r.error ?? "resume failed");
      }
    } catch (e) {
      setError(String(e));
    }
  };

  // DirBrowser / ResumePicker 가 확정되면 즉시 RPC 가 아니라 permission 모드로 전이.
  // pending 에 "어떤 RPC 를 어떤 파라미터로 칠지" 만 보관하고, PermissionPrompt 가
  // skipPermissions 결정을 합쳐 do{Spawn,Resume} 호출.
  const onCwdConfirmed = (cwd: string): void => {
    setPending({ kind: "new", cwd });
    setMode("permission");
  };

  const onResumeSelected = (id: string): void => {
    setPending({ kind: "resume", id });
    setMode("permission");
  };

  const onPermissionChoose = (skipPermissions: boolean): void => {
    const p = pending;
    setPending(undefined);
    setMode("sessions");
    if (!p) return;
    if (p.kind === "new") void doSpawn(p.cwd, skipPermissions);
    else void doResume(p.id, skipPermissions);
  };

  const onPermissionCancel = (): void => {
    setPending(undefined);
    setMode("sessions");
  };

  const onKill = (s: CliSessionInfo): void => {
    setInfo(`▶ killing [${s.id}] ${s.label}...`);
    rpc({ op: "cli_request", command: "kill", target: s.id })
      .then((r) => {
        if (r.ok && r.data?.kind === "kill") {
          if (r.data.success) {
            setInfo(`✓ killed ${s.id}`);
          } else {
            setError(r.data.message ?? "kill failed");
          }
        } else {
          setError(r.error ?? "kill failed");
        }
      })
      .catch((e) => setError(String(e)));
    // 사용자가 menu 안에 있으니 tmux session 도 같이 정리됨 (handler 가 처리). 추가 안전장치:
    if (s.tmuxName) killTmuxSession(s.tmuxName);
  };

  const onDisconnect = (): void => {
    disconnectSelf();
  };

  return (
    // 터미널 전체 높이를 외곽 Box 가 점유하고, content 영역이 flexGrow 로 잔여 공간을
    // 차지. 자식 picker (DirBrowser/ResumePicker) 가 그 안에서 measureElement 로 list
    // 가용 높이를 측정 → chrome 줄 수를 하드코딩으로 빼지 않고 ink layout 이 분배.
    <Box flexDirection="column" padding={1} height={termRows}>
      <Box>
        <Text color="green" bold>cb-menu </Text>
        <Text dimColor>· {formatMinimap(sessions, spin)}</Text>
      </Box>
      {error && (
        <Box marginTop={1}>
          <Text color="red">✗ {error}</Text>
        </Box>
      )}
      {info && (
        <Box marginTop={1}>
          <Text color="yellow">{info}</Text>
        </Box>
      )}
      <Box marginTop={1} flexGrow={1}>
        {mode === "sessions" && (
          <SessionList
            sessions={sessions}
            onSelect={onSelect}
            onNew={() => setMode("new")}
            onResume={() => setMode("resume")}
            onKill={onKill}
            onDisconnect={onDisconnect}
          />
        )}
        {mode === "new" && (
          <DirBrowser
            onConfirm={onCwdConfirmed}
            onCancel={() => setMode("sessions")}
          />
        )}
        {mode === "resume" && (
          <ResumePicker
            onSelect={onResumeSelected}
            onCancel={() => setMode("sessions")}
          />
        )}
        {mode === "permission" && pending && (
          <PermissionPrompt
            summary={
              pending.kind === "new"
                ? `▶ new session at ${pending.cwd}`
                : `▶ resume [${pending.id}]`
            }
            onChoose={onPermissionChoose}
            onCancel={onPermissionCancel}
          />
        )}
      </Box>
    </Box>
  );
}

async function waitForTmuxAndSwitch(
  tmuxName: string,
  onError: (msg: string) => void,
): Promise<void> {
  const { spawnSync } = await import("node:child_process");
  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline) {
    const has = spawnSync("tmux", ["has-session", "-t", `=${tmuxName}`]);
    if (has.status === 0) {
      const r = switchToSession(tmuxName);
      if (r.code !== 0) onError(`switch-client 실패: ${r.stderr.trim()}`);
      return;
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  onError(`tmux session ${tmuxName} 가 5초 내에 나타나지 않음`);
}
