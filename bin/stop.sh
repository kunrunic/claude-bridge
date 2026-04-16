#!/bin/bash
# claude-bridge 종료
#
# 사용:
#   ./bin/stop.sh           봇만 종료 (기본) — tmux/Claude 는 독립 유지, 재기동 시 자동 재연결
#   ./bin/stop.sh --all     tmux 세션과 Claude 까지 함께 종료 (Claude 에 /exit 먼저 보내고 graceful wait)
set -e

cd "$(dirname "$0")/.."

PID_FILE=".bot.pid"

INSTANCE_NAME=$(basename "$PWD")

# 옵션 파싱
STOP_ALL=0
for arg in "$@"; do
    case "$arg" in
        --all) STOP_ALL=1 ;;
        -h|--help)
            echo "사용: ./bin/stop.sh [--all]"
            echo "  (기본)   봇만 종료 — tmux/Claude 는 독립 유지 (재기동 시 자동 재연결)"
            echo "  --all    tmux 세션과 Claude 까지 함께 종료"
            exit 0
            ;;
    esac
done

# ── 봇 종료 ──────────────────────────────────────────────────
if [ ! -f "$PID_FILE" ]; then
    echo "실행 중인 봇 없음 (PID 파일 없음)."
    # 해당 인스턴스명으로만 정확히 검색 (다른 Python 프로세스 절대 안전)
    LEFTOVER=$(pgrep -f "bot\.py $INSTANCE_NAME\$" | head -1 || true)
    if [ -n "$LEFTOVER" ]; then
        echo "이 폴더의 남은 프로세스 발견 (PID=$LEFTOVER). 종료합니다."
        kill $LEFTOVER 2>/dev/null || true
    fi
    # --all 이면 봇이 없어도 tmux 정리까지 진행
    if [ "$STOP_ALL" != "1" ]; then
        exit 0
    fi
else
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
fi

# ── 락파일 정리 (이 인스턴스 소유만) ──────────────────────────
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

# ── --all: tmux 세션 + Claude 종료 ───────────────────────────
if [ "$STOP_ALL" = "1" ] && [ -n "$TMUX_NAME" ]; then
    TMUX_BIN=$(command -v tmux || echo "/opt/homebrew/bin/tmux")
    if $TMUX_BIN has-session -t "$TMUX_NAME" 2>/dev/null; then
        echo "tmux 세션 정리 중 (Claude graceful exit 유도)..."
        # Claude 에 /exit 전송 → graceful shutdown 으로 세션 JSONL 정상 저장
        $TMUX_BIN send-keys -t "$TMUX_NAME" -l "/exit" 2>/dev/null || true
        $TMUX_BIN send-keys -t "$TMUX_NAME" Enter 2>/dev/null || true
        sleep 5
        $TMUX_BIN kill-session -t "$TMUX_NAME" 2>/dev/null || true
        echo "tmux 세션 종료됨 ($TMUX_NAME)"
    else
        echo "tmux 세션 없음 ($TMUX_NAME) — 스킵"
    fi
fi
