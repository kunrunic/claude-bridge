#!/usr/bin/env bash
# claude-bridge stop (MCP edition)
#
# 기본: 디스패처만 종료. 실행 중인 Claude tmux 세션(cb-s*)은 그대로 유지.
# --all: cb-s* tmux 세션까지 모두 정리.

set -euo pipefail

cd "$(dirname "$0")/.."

CB_HOME="${CB_HOME:-$HOME/.claude-bridge}"
PID_FILE="$CB_HOME/telegram/bot.pid"

STOP_ALL=0
for arg in "$@"; do
  case "$arg" in
    --all) STOP_ALL=1 ;;
    -h|--help)
      cat <<'USAGE'
usage: ./bin/stop.sh [--all]
  (default)  stop dispatcher only. cb-s* tmux sessions survive
             (reconnected automatically on next start).
  --all      stop dispatcher AND kill every cb-s* tmux session.
             sends /exit to each pane first, waits briefly, then kill-session.
USAGE
      exit 0
      ;;
    *)
      echo "unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

# 1. dispatcher
if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      if ! kill -0 "$PID" 2>/dev/null; then break; fi
      sleep 1
    done
    if kill -0 "$PID" 2>/dev/null; then
      kill -9 "$PID" 2>/dev/null || true
      echo "✗ forced SIGKILL (PID=$PID)"
    else
      echo "✓ stopped (PID=$PID)"
    fi
  else
    echo "PID file stale (PID=$PID not running)"
  fi
  rm -f "$PID_FILE"
else
  echo "no PID file — dispatcher not running"
fi

# 2. --all: kill cb-s* tmux sessions
if [ "$STOP_ALL" = "1" ]; then
  TMUX_BIN=$(command -v tmux || echo "/opt/homebrew/bin/tmux")
  if ! command -v "$TMUX_BIN" >/dev/null 2>&1; then
    echo "✗ tmux not found — skip --all" >&2
    exit 0
  fi

  mapfile -t SESSIONS < <("$TMUX_BIN" list-sessions -F "#{session_name}" 2>/dev/null | grep '^cb-' || true)
  if [ ${#SESSIONS[@]} -eq 0 ]; then
    echo "no cb-* tmux sessions to clean"
    exit 0
  fi

  for s in "${SESSIONS[@]}"; do
    echo "→ graceful /exit to $s"
    "$TMUX_BIN" send-keys -t "$s" -l "/exit" 2>/dev/null || true
    "$TMUX_BIN" send-keys -t "$s" Enter 2>/dev/null || true
  done
  sleep 3
  for s in "${SESSIONS[@]}"; do
    if "$TMUX_BIN" has-session -t "$s" 2>/dev/null; then
      "$TMUX_BIN" kill-session -t "$s" 2>/dev/null || true
      echo "✓ killed $s"
    fi
  done
fi
