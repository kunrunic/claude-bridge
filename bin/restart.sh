#!/bin/bash
# claude-bridge 재시작 (stop → start)
set -e

HERE="$(dirname "$0")"
cd "$HERE/.."

echo "=== 재시작 시작 ==="
"$HERE/stop.sh" || true
echo "--- 시작 ---"
"$HERE/start.sh" "$@"
