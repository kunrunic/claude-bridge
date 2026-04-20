#!/usr/bin/env bash
# claude-bridge state snapshot (MCP edition)
#
# SSH 로 접속해 현재 상태를 빠르게 확인하는 도구.
# 기본: 디스패처 PID + 활성 cb-s* 세션 목록 + 최근 anomaly 20줄
# --session <name>  : 해당 tmux pane 마지막 N줄 capture
# --follow <name>   : tmux attach -r (read-only)
# --save            : capture/YYYYMMDD_HHMMSS.txt 에 저장
# -n N              : capture line count (default 500)

set -euo pipefail

cd "$(dirname "$0")/.."

CB_HOME="${CB_HOME:-$HOME/.claude-bridge}"
PID_FILE="$CB_HOME/telegram/bot.pid"
ANOMALY_LOG="$CB_HOME/anomaly.jsonl"
REGISTRY_PATH="$CB_HOME/registry.json"

LINES=500
RAW=0
SAVE=0
FOLLOW=0
TARGET=""
# default overview 에 각 cb-* pane tail 도 포함 — 세션 이름만 봐서는 진단이
# 안 되니. --no-panes 로 끌 수 있음. tail 기본 15줄.
PANES=1
PANE_TAIL=15

while [ $# -gt 0 ]; do
  case "$1" in
    -n|--lines) LINES="$2"; shift 2 ;;
    --raw)      RAW=1; shift ;;
    --save)     SAVE=1; shift ;;
    -f|--follow) FOLLOW=1; shift ;;
    --session)  TARGET="$2"; shift 2 ;;
    --no-panes) PANES=0; shift ;;
    --pane-tail) PANE_TAIL="$2"; shift 2 ;;
    -h|--help)
      cat <<'USAGE'
usage: ./bin/capture.sh [options]
  (no args)                overview: dispatcher + tmux + registry + 각 pane tail + anomalies
  --session <cb-sN>        capture that tmux pane (last N lines)
  -n N                     line count for --session capture (default 500)
  --raw                    keep ANSI escape codes
  --save                   save to capture/YYYYMMDD_HHMMSS.txt instead of stdout
  --no-panes               overview 에서 pane tail 섹션 생략
  --pane-tail N            overview 각 pane 에 보여줄 tail 줄 수 (default 15)
  -f | --follow --session <name>
                           read-only tmux attach (detach: Ctrl+b then d)
USAGE
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      echo "try --help" >&2
      exit 1
      ;;
  esac
done

TMUX_BIN=$(command -v tmux || echo "/opt/homebrew/bin/tmux")

# ── follow mode ─────────────────────────────────────────────────
if [ "$FOLLOW" = "1" ]; then
  if [ -z "$TARGET" ]; then
    echo "✗ --follow requires --session <cb-sN>" >&2
    exit 1
  fi
  if ! "$TMUX_BIN" has-session -t "$TARGET" 2>/dev/null; then
    echo "✗ no such tmux session: $TARGET" >&2
    exit 1
  fi
  echo "=> read-only attach to $TARGET"
  echo "   detach: Ctrl+b (release) then d"
  exec "$TMUX_BIN" attach -t "$TARGET" -r
fi

# ── specific session capture ────────────────────────────────────
if [ -n "$TARGET" ]; then
  if ! "$TMUX_BIN" has-session -t "$TARGET" 2>/dev/null; then
    echo "✗ no such tmux session: $TARGET" >&2
    echo "  active cb-* sessions:" >&2
    "$TMUX_BIN" list-sessions -F "    #{session_name}" 2>/dev/null | grep '^ *cb-' >&2 || echo "    (none)" >&2
    exit 1
  fi
  ARGS=(capture-pane -t "$TARGET" -p -S "-${LINES}")
  [ "$RAW" = "1" ] && ARGS=(capture-pane -t "$TARGET" -e -p -S "-${LINES}")
  if [ "$SAVE" = "1" ]; then
    mkdir -p capture
    TS=$(date +%Y%m%d_%H%M%S)
    OUT="capture/${TS}_${TARGET}.txt"
    {
      echo "# claude-bridge tmux capture"
      echo "# session: $TARGET"
      echo "# taken:   $(date '+%Y-%m-%d %H:%M:%S')"
      echo "# lines:   $LINES"
      echo
      "$TMUX_BIN" "${ARGS[@]}"
    } > "$OUT"
    echo "$OUT"
  else
    "$TMUX_BIN" "${ARGS[@]}"
  fi
  exit 0
fi

# ── default: status overview ────────────────────────────────────
echo "── dispatcher ──"
if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    echo "  PID=$PID (running)"
  else
    echo "  PID=$PID (stale — file exists but process gone)"
  fi
else
  echo "  not running (no PID file)"
fi

echo
echo "── active tmux sessions (cb-*) ──"
"$TMUX_BIN" list-sessions -F "  #{session_name}  created=#{t:session_created}  windows=#{session_windows}" 2>/dev/null | grep 'cb-' || echo "  (none)"

echo
echo "── registry state ──"
if [ -f "$REGISTRY_PATH" ]; then
  # 우선 bun 기반 JSON pretty parse 를 시도, 없으면 raw cat
  if command -v bun >/dev/null 2>&1; then
    bun -e "
      const r = JSON.parse(require('node:fs').readFileSync('$REGISTRY_PATH','utf-8'));
      if (!r.sessions.length) { console.log('  (empty)'); process.exit(0); }
      console.log('  activeId: ' + (r.activeId ?? '(none)'));
      for (const s of r.sessions) {
        const marker = (s.id === r.activeId) ? '*' : ' ';
        console.log('  ' + marker + ' ' + s.id + ' (' + s.label + ')  tmux=' + s.tmuxName + '  state=' + s.state + '  signal=' + s.signal);
      }
    " 2>/dev/null || cat "$REGISTRY_PATH"
  else
    cat "$REGISTRY_PATH"
  fi
else
  echo "  (no registry yet at $REGISTRY_PATH)"
fi

if [ "$PANES" = "1" ]; then
  echo
  echo "── pane tails (last $PANE_TAIL lines each) ──"
  CB_SESSIONS=$("$TMUX_BIN" list-sessions -F "#{session_name}" 2>/dev/null | grep '^cb-' || true)
  if [ -z "$CB_SESSIONS" ]; then
    echo "  (no cb-* sessions to capture)"
  else
    for S in $CB_SESSIONS; do
      echo
      echo "  ── $S ──"
      "$TMUX_BIN" capture-pane -t "$S" -p -J -S "-${PANE_TAIL}" 2>/dev/null | sed 's/^/    /' || echo "    (capture failed)"
    done
  fi
fi

echo
echo "── recent anomalies (last 20) ──"
if [ -f "$ANOMALY_LOG" ]; then
  tail -n 20 "$ANOMALY_LOG"
else
  echo "  (no log yet at $ANOMALY_LOG)"
fi

echo
echo "── tip ──"
echo "  ./bin/capture.sh --session cb-s1              # last ${LINES} lines of that pane"
echo "  ./bin/capture.sh --follow --session cb-s1     # live read-only view"
