#!/bin/bash
# claude-bridge 백그라운드 시작
set -e

cd "$(dirname "$0")"

PID_FILE=".bot.pid"
LOG_DIR="logs"
LOG_FILE="$LOG_DIR/$(date +%Y-%m-%d).log"

# 중복 실행 확인
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 $PID 2>/dev/null; then
        echo "이미 실행 중 (PID=$PID). ./stop.sh 먼저 실행하세요."
        exit 1
    else
        rm -f "$PID_FILE"
    fi
fi

# venv 확인
if [ ! -d "venv" ]; then
    echo "venv 없음. ./setup.sh 먼저 실행하세요."
    exit 1
fi

# config.json 확인
if [ ! -f "config.json" ]; then
    echo "config.json 없음. ./setup.sh 먼저 실행하세요."
    exit 1
fi

# 로그 디렉토리 준비
mkdir -p "$LOG_DIR"

# 3일 이상 된 로그 삭제
find "$LOG_DIR" -name "*.log" -type f -mtime +3 -delete 2>/dev/null || true

# 백그라운드 실행
source venv/bin/activate
nohup python -u bot.py >> "$LOG_FILE" 2>&1 &
BOT_PID=$!

echo $BOT_PID > "$PID_FILE"

sleep 1
if kill -0 $BOT_PID 2>/dev/null; then
    echo "✓ 실행됨 (PID=$BOT_PID)"
    echo "  로그: $LOG_FILE"
    echo "  종료: ./stop.sh"
else
    echo "✗ 실행 실패. $LOG_FILE 확인"
    rm -f "$PID_FILE"
    exit 1
fi
