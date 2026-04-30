#!/usr/bin/env bash
# claude-bridge start (MCP edition)
#
# 기본: 백그라운드 기동, 로그는 ~/.claude-bridge/logs/YYYY-MM-DD.log
# --fg: 전경 실행 (Ctrl+C 로 종료)

set -euo pipefail

cd "$(dirname "$0")/.."

CB_HOME="${CB_HOME:-$HOME/.claude-bridge}"
CONFIG_PATH="$CB_HOME/config.json"
# dispatcher process 추적용 PID 파일 — 모드(bridge/native)와 무관.
# Telegram polling lock 의 PID (CB_HOME/telegram/bot.pid) 와는 책임이 다르다:
#  · dispatcher.pid : start.sh / stop.sh 가 dispatcher 프로세스 자체를 추적
#  · telegram/bot.pid : TelegramChannel.acquirePollingLock 이 같은 봇 토큰
#                       중복 폴링 방지용 (bridge 모드에서만 생성됨)
PID_FILE="$CB_HOME/dispatcher.pid"
LOG_DIR="$CB_HOME/logs"

FOREGROUND=0
for arg in "$@"; do
  case "$arg" in
    --fg|--foreground) FOREGROUND=1 ;;
    -h|--help)
      cat <<'USAGE'
usage: ./bin/start.sh [--fg]
  --fg   run in foreground (Ctrl+C to stop). default is background.
logs at ~/.claude-bridge/logs/YYYY-MM-DD.log
USAGE
      exit 0
      ;;
    *)
      echo "unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

# config present?
if [ ! -f "$CONFIG_PATH" ]; then
  echo "✗ config not found: $CONFIG_PATH" >&2
  echo "  run ./bin/setup.sh first" >&2
  exit 1
fi

# already running?
if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    echo "already running (PID=$PID). stop with ./bin/stop.sh" >&2
    exit 1
  fi
  rm -f "$PID_FILE"
fi

mkdir -p "$CB_HOME" "$LOG_DIR"
# prune logs > 7 days
find "$LOG_DIR" -maxdepth 1 -name "*.log" -type f -mtime +7 -delete 2>/dev/null || true

LOG_FILE="$LOG_DIR/$(date +%Y-%m-%d).log"

if [ "$FOREGROUND" = "1" ]; then
  exec bun run src/dispatcher.ts
fi

# background
nohup bun run src/dispatcher.ts >> "$LOG_FILE" 2>&1 &
BOT_PID=$!
disown "$BOT_PID" 2>/dev/null || true
echo "$BOT_PID" > "$PID_FILE"

sleep 1
if kill -0 "$BOT_PID" 2>/dev/null; then
  echo "✓ started (PID=$BOT_PID)"
  echo "  log:  $LOG_FILE"
  echo "  stop: ./bin/stop.sh"
else
  echo "✗ failed to start — see $LOG_FILE" >&2
  rm -f "$PID_FILE"
  exit 1
fi
