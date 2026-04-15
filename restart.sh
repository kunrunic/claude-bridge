#!/bin/bash
# claude-bridge 재시작 (stop → start)
set -e

cd "$(dirname "$0")"

echo "=== 재시작 시작 ==="
./stop.sh || true
echo "--- 시작 ---"
./start.sh
