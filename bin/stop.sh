#!/usr/bin/env bash
# claude-bridge stop
# dispatcher 프로세스를 종료한다.
# cb-* tmux 세션은 다음 시작 시 reconcileOrphans 가 정리한다.

set -euo pipefail

cd "$(dirname "$0")/.."

CB_HOME="${CB_HOME:-$HOME/.claude-bridge}"
PID_FILE="$CB_HOME/telegram/bot.pid"

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
