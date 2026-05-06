#!/usr/bin/env bash
# claude-bridge lag snapshot
#
# cb 입력 lag 이 발생한 순간에 한 번 실행하면, 그 시점의 instantaneous CPU,
# dispatcher / cb-menu 의 sample stack, tmux 상태, 네트워크 RTT, 로그 tail 을
# lag-snapshot/<TS>-<host>/ 에 한꺼번에 저장한다.
#
# 진단 의도:
#   ps 의 %CPU 는 lifetime 평균이라 startup 비용이 누적돼 보일 수 있음.
#   sample stack 은 그 순간 프로세스가 정말 일하고 있는지 (kevent sleep 인지)
#   를 즉시 보여주므로 lag 의 진짜 책임자가 누군지 분리할 때 유용.
#
# 사용:
#   ./bin/cb-lag-snapshot.sh home    # ssh-resolvable 원격 호스트
#   ./bin/cb-lag-snapshot.sh         # 로컬 머신

set -euo pipefail

cd "$(dirname "$0")/.."

HOST="${1:-}"
case "${HOST}" in
  -h|--help)
    cat <<'USAGE'
usage: ./bin/cb-lag-snapshot.sh [host]
  host   ssh-resolvable host name (예: 'home'). 생략 시 로컬 머신.

수집 항목:
  · 네트워크 RTT (ping 5회 — 원격 한정)
  · top -l 1 instantaneous CPU (top 10)
  · cb-menu / dispatcher / telegram-server / claude TUI ps
  · dispatcher + cb-menu 의 sample(2s) stack
  · tmux 세션 및 pane 목록
  · 오늘자 dispatcher 로그 tail 50줄

출력: lag-snapshot/YYYYMMDD_HHMMSS-<host>/
USAGE
    exit 0
    ;;
esac

TS=$(date +%Y%m%d_%H%M%S)
LABEL="${HOST:-local}"
OUT="lag-snapshot/${TS}-${LABEL}"
mkdir -p "$OUT"

printf '==> lag snapshot — target=%s\n    out=%s\n\n' "$LABEL" "$OUT"

# ── 1. 네트워크 RTT (원격 한정) ─────────────────────────────────
if [ -n "$HOST" ]; then
  echo "[1/3] ping RTT (5회)..."
  ping -c 5 -i 0.3 "$HOST" > "$OUT/rtt.txt" 2>&1 || true
  tail -2 "$OUT/rtt.txt" 2>/dev/null || true
  echo
fi

# ── 2. 원격/로컬 단일 bash 세션으로 수집 ─────────────────────
# heredoc 'SCRIPT' (quoted) — 로컬 변수 expansion 비활성, $HOME / $(date) 는
# 원격 bash 가 실행 시점에 평가. ssh stdin 으로 흘려보내면 quoting 무관.
echo "[2/3] procs / sample / tmux / log..."
TMP_SCRIPT=$(mktemp)
trap 'rm -f "$TMP_SCRIPT"' EXIT
cat > "$TMP_SCRIPT" <<'SCRIPT'
echo "=== top (instantaneous, sorted by CPU, top 10) ==="
top -l 1 -n 10 -o cpu 2>/dev/null | sed -n "1,30p"
echo
echo "=== uptime ==="
uptime
echo
echo "=== cb-related processes ==="
ps -axo pid,pcpu,pmem,etime,command 2>/dev/null \
  | grep -E "src/dispatcher.ts|menu/index.tsx|telegram/server.ts| claude " \
  | grep -v grep || echo "(none)"
echo
echo "=== tmux ==="
if command -v tmux >/dev/null 2>&1; then
  tmux list-sessions 2>&1 || echo "(no tmux server)"
  echo
  tmux list-panes -a -F "#{session_name}:#{window_index}.#{pane_index} pid=#{pane_pid} cmd=#{pane_current_command}" 2>&1 || true
else
  echo "(tmux not found)"
fi
echo
echo "=== sample stacks (2s each — dispatcher + cb-menu) ==="
PIDS="$(pgrep -f 'src/dispatcher.ts' 2>/dev/null) $(pgrep -f 'menu/index.tsx' 2>/dev/null)"
PIDS="$(echo "$PIDS" | tr -s ' ' '\n' | grep -v '^$' | sort -u)"
if [ -z "$PIDS" ]; then
  echo "(no dispatcher / cb-menu pids)"
else
  for pid in $PIDS; do
    echo "--- pid=$pid ---"
    sample "$pid" 2 -mayDie 2>/dev/null | sed -n "1,80p" || echo "(sample failed)"
    echo
  done
fi
echo "=== dispatcher log tail (50) ==="
LOG_FILE="$HOME/.claude-bridge/logs/$(date +%Y-%m-%d).log"
if [ -f "$LOG_FILE" ]; then
  tail -50 "$LOG_FILE"
else
  echo "(no log: $LOG_FILE)"
fi
SCRIPT

if [ -n "$HOST" ]; then
  ssh -o BatchMode=yes "$HOST" bash -s < "$TMP_SCRIPT" > "$OUT/snapshot.txt" 2>&1 || true
else
  bash "$TMP_SCRIPT" > "$OUT/snapshot.txt" 2>&1 || true
fi

# ── 3. 요약 ─────────────────────────────────────────────────
echo "[3/3] summary"
echo
echo "--- cb procs ---"
awk '/=== cb-related processes ===/{f=1;next} /^=== /{f=0} f' "$OUT/snapshot.txt" | head -15 || true
echo
if [ -f "$OUT/rtt.txt" ]; then
  echo "--- rtt ---"
  tail -2 "$OUT/rtt.txt"
fi

echo
echo "==> saved"
ls -la "$OUT/"
