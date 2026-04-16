#!/bin/bash
# claude-bridge tmux 패널 캡처 도구
# 서버에 SSH 접속해 현재 bridge 의 Claude 상태를 빠르게 확인할 때 사용.
#
# 사용:
#   ./bin/capture.sh                     현재 패널 마지막 500줄 출력 (기본)
#   ./bin/capture.sh -n 1000             줄 수 지정
#   ./bin/capture.sh --raw               ANSI 색상 코드 포함 (터미널 컬러 유지)
#   ./bin/capture.sh --save              capture/YYYYMMDD_HHMMSS.txt 에 저장 후 경로 출력
#   ./bin/capture.sh -f | --follow       tmux attach -r (read-only) — 실시간 관찰, Ctrl+B d 로 detach
#
# --follow 는 read-only 모드라 키 입력이 bridge 에 전달되지 않아 안전.
set -e

cd "$(dirname "$0")/.."

LINES=500
RAW=0
SAVE=0
FOLLOW=0

while [ $# -gt 0 ]; do
    case "$1" in
        -n|--lines)
            LINES="$2"; shift 2 ;;
        --raw)
            RAW=1; shift ;;
        --save)
            SAVE=1; shift ;;
        -f|--follow)
            FOLLOW=1; shift ;;
        -h|--help)
            cat <<'USAGE'
claude-bridge tmux 패널 캡처 도구
서버 SSH 로 접속해 현재 bridge 의 Claude 상태를 빠르게 확인할 때.

사용:
  ./bin/capture.sh                 현재 패널 마지막 500줄 출력 (ANSI 제거)
  ./bin/capture.sh -n 1000         줄 수 지정
  ./bin/capture.sh --raw           ANSI 색상 코드 포함
  ./bin/capture.sh --save          capture/YYYYMMDD_HHMMSS.txt 에 저장하고 경로만 출력
  ./bin/capture.sh -f / --follow   tmux attach -r (read-only) — 실시간 관찰
                                   (Ctrl+B d 로 detach, 키 입력은 bridge 에 전달 안 됨)
USAGE
            exit 0
            ;;
        *)
            echo "알 수 없는 옵션: $1"
            echo "도움말: ./bin/capture.sh --help"
            exit 1
            ;;
    esac
done

# ── tmux 세션명 결정 ─────────────────────────────────────────
TMUX_BIN=$(command -v tmux || echo "/opt/homebrew/bin/tmux")
TMUX_NAME=$(python3 -c "import json; print(json.load(open('config.json')).get('tmux_session','claude_bridge'))" 2>/dev/null || echo "claude_bridge")

if ! $TMUX_BIN has-session -t "$TMUX_NAME" 2>/dev/null; then
    echo "tmux 세션 없음 ($TMUX_NAME). ./bin/start.sh 로 봇을 먼저 시작하세요."
    echo
    echo "현재 tmux 세션 목록:"
    $TMUX_BIN list-sessions 2>/dev/null || echo "  (tmux 서버 없음)"
    exit 1
fi

# ── --follow: read-only attach ───────────────────────────────
if [ "$FOLLOW" = "1" ]; then
    echo "=> tmux attach -t $TMUX_NAME -r (read-only)"
    echo "   Ctrl+B d  로 detach 하세요. 키 입력은 bridge 에 전달되지 않습니다."
    echo
    exec $TMUX_BIN attach -t "$TMUX_NAME" -r
fi

# ── capture-pane ─────────────────────────────────────────────
# tmux capture-pane 기본은 ANSI 없는 plain text. -e 붙이면 ANSI 유지.
CAPTURE_ARGS=(capture-pane -t "$TMUX_NAME" -p -S "-${LINES}")
if [ "$RAW" = "1" ]; then
    CAPTURE_ARGS=(capture-pane -t "$TMUX_NAME" -e -p -S "-${LINES}")
fi

if [ "$SAVE" = "1" ]; then
    mkdir -p capture
    TS=$(date +%Y%m%d_%H%M%S)
    OUT="capture/${TS}.txt"
    {
        echo "# claude-bridge tmux capture"
        echo "# session: $TMUX_NAME"
        echo "# taken:   $(date '+%Y-%m-%d %H:%M:%S')"
        echo "# lines:   $LINES"
        echo "# raw:     $RAW"
        echo
        $TMUX_BIN "${CAPTURE_ARGS[@]}"
    } > "$OUT"
    echo "$OUT"
else
    $TMUX_BIN "${CAPTURE_ARGS[@]}"
fi
