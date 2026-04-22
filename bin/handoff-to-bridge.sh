#!/usr/bin/env bash
# Hand off the current local claude session to claude-bridge (tmux-managed).
# Extracts the current session ID from the parent claude process (`--resume` arg),
# then sends a `spawn_request` over the dispatcher's Unix socket.
#
# This script does NOT talk to any front-end channel (Telegram/etc.). It only
# writes to dispatcher.sock; the dispatcher then spawns the tmux session and
# whatever channel adapters are attached (Telegram today, others possible)
# surface the result to the user.
#
# After success, the bridge spawns `claude --resume <sessionId>`. The local
# session is NOT auto-exited — /exit yourself to avoid concurrent writes to
# the same session JSONL.

set -euo pipefail

SOCKET="${CB_DISPATCHER_SOCKET:-$HOME/.claude-bridge/dispatcher.sock}"
CWD="$(pwd)"

if [ ! -S "$SOCKET" ]; then
  echo "✗ dispatcher not running (socket missing: $SOCKET)" >&2
  echo "  start with: $(dirname "$0")/start.sh" >&2
  exit 1
fi

# Find the current claude session ID by walking up the process tree and
# looking for a claude process with --resume <uuid>.
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

# Send JSON + newline to the dispatcher socket. Fire-and-forget — dispatcher
# handles the announce via its own channel adapters; this script doesn't wait
# or know which channel is attached.
printf '%s\n' "$PAYLOAD" | nc -U -w 1 "$SOCKET" >/dev/null || {
  echo "✗ failed to send spawn_request to dispatcher" >&2
  exit 1
}

echo "✓ handoff 요청됨 — bridge 에서 spawn 확인 후 /exit 로 이 세션을 종료하세요."
echo "  (양쪽 세션에서 동시 입력하면 히스토리가 섞입니다)"
