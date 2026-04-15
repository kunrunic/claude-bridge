#!/bin/bash
# claude-bridge 종료
set -e

cd "$(dirname "$0")"

PID_FILE=".bot.pid"

INSTANCE_NAME=$(basename "$PWD")

if [ ! -f "$PID_FILE" ]; then
    echo "실행 중인 봇 없음 (PID 파일 없음)."
    # 해당 인스턴스명으로만 정확히 검색 (다른 Python 프로세스 절대 안전)
    LEFTOVER=$(pgrep -f "bot\.py $INSTANCE_NAME\$" | head -1 || true)
    if [ -n "$LEFTOVER" ]; then
        echo "이 폴더의 남은 프로세스 발견 (PID=$LEFTOVER). 종료합니다."
        kill $LEFTOVER 2>/dev/null || true
    fi
    exit 0
fi

PID=$(cat "$PID_FILE")

if kill -0 $PID 2>/dev/null; then
    kill $PID
    sleep 5
    if kill -0 $PID 2>/dev/null; then
        echo "정상 종료 안됨, 강제 종료 중..."
        kill -9 $PID 2>/dev/null || true
    fi
    echo "✓ 종료됨 (PID=$PID)"
else
    echo "PID=$PID 프로세스 없음 (이미 종료된 것으로 보임)"
fi

rm -f "$PID_FILE"

# 이 인스턴스 소유 락파일 정리
TMUX_NAME=$(python3 -c "import json; print(json.load(open('config.json')).get('tmux_session','claude_bridge'))" 2>/dev/null || echo "")
if [ -n "$TMUX_NAME" ]; then
    COUNT=0
    for lf in ~/.claude/.cb_lock_*; do
        [ -f "$lf" ] || continue
        OWNER=$(head -1 "$lf" 2>/dev/null || true)
        if [ "$OWNER" = "$TMUX_NAME" ]; then
            rm -f "$lf"
            COUNT=$((COUNT + 1))
        fi
    done
    [ "$COUNT" -gt 0 ] && echo "✓ 락파일 ${COUNT}개 해제"
fi
