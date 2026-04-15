#!/bin/bash
# claude-bridge 종료
set -e

cd "$(dirname "$0")"

PID_FILE=".bot.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "실행 중인 봇 없음 (PID 파일 없음)."
    # 혹시 남은 프로세스 찾아서 종료
    LEFTOVER=$(pgrep -f "python.*bot.py" | head -1 || true)
    if [ -n "$LEFTOVER" ]; then
        echo "남은 프로세스 발견 (PID=$LEFTOVER). 종료합니다."
        kill $LEFTOVER 2>/dev/null || true
    fi
    exit 0
fi

PID=$(cat "$PID_FILE")

if kill -0 $PID 2>/dev/null; then
    kill $PID
    sleep 1
    if kill -0 $PID 2>/dev/null; then
        echo "정상 종료 안됨, 강제 종료 중..."
        kill -9 $PID 2>/dev/null || true
    fi
    echo "✓ 종료됨 (PID=$PID)"
else
    echo "PID=$PID 프로세스 없음 (이미 종료된 것으로 보임)"
fi

rm -f "$PID_FILE"
