#!/usr/bin/env bash
# Hand off the current SSH tmux session to Telegram (claude-bridge).
#
# Two modes:
#   1. CB_SESSION_ID is set (running inside a cb-managed tmux session):
#      Send set_active_request — no re-spawn, current session becomes Telegram active.
#   2. CB_SESSION_ID is not set (standalone claude process):
#      Extract session ID from --resume arg, then send spawn_request to create
#      a new bridge-mode session (resume). Original session needs /exit.

set -euo pipefail

SOCKET="${CB_DISPATCHER_SOCKET:-$HOME/.claude-bridge/dispatcher.sock}"
CWD="$(pwd)"

if [ ! -S "$SOCKET" ]; then
  echo "✗ dispatcher not running (socket missing: $SOCKET)" >&2
  echo "  start with: $(dirname "$0")/start.sh" >&2
  exit 1
fi

# ── Mode 1: already inside a cb-managed session ───────────────────────────────
if [ -n "${CB_SESSION_ID:-}" ]; then
  PAYLOAD="{\"op\":\"set_active_request\",\"session_id\":\"$CB_SESSION_ID\"}"
  printf '%s\n' "$PAYLOAD" | nc -U -w 1 "$SOCKET" >/dev/null || {
    echo "✗ failed to send set_active_request to dispatcher" >&2
    exit 1
  }
  echo "✓ 이 세션($CB_SESSION_ID)을 Telegram active로 전환했습니다."
  echo "  이제 Telegram에서 이 세션으로 메시지를 보낼 수 있습니다."
  exit 0
fi

# ── Mode 2: standalone claude (not in cb tmux) ────────────────────────────────
SESSION_ID=""
pid=$PPID
for _ in 1 2 3 4 5; do
  [ -z "$pid" ] && break
  args=$(ps -o args= -p "$pid" 2>/dev/null || true)
  case "$args" in
    *"--resume "*)
      SESSION_ID=$(echo "$args" | grep -oE -- '--resume [0-9a-f-]{36}' | awk '{print $2}' | head -1)
      [ -n "$SESSION_ID" ] && break
      ;;
  esac
  pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)
done

if [ -z "$SESSION_ID" ]; then
  PAYLOAD="{\"op\":\"spawn_request\",\"cwd\":\"$CWD\"}"
  echo "⚠ current claude session ID not detected — will spawn fresh session in $CWD"
else
  PAYLOAD="{\"op\":\"spawn_request\",\"cwd\":\"$CWD\",\"resumeId\":\"$SESSION_ID\"}"
  echo "→ handoff session=$SESSION_ID cwd=$CWD"
fi

printf '%s\n' "$PAYLOAD" | nc -U -w 1 "$SOCKET" >/dev/null || {
  echo "✗ failed to send spawn_request to dispatcher" >&2
  exit 1
}

echo "✓ handoff 요청됨 — bridge 에서 spawn 확인 후 /exit 로 이 세션을 종료하세요."
echo "  (양쪽 세션에서 동시 입력하면 히스토리가 섞입니다)"
