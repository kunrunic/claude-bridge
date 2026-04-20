#!/usr/bin/env bash
# claude-bridge restart — thin wrapper over stop + start.
# args pass through to start.sh (e.g. --fg).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

echo "=== stop ==="
"$HERE/stop.sh" || true
echo "=== start ==="
"$HERE/start.sh" "$@"
