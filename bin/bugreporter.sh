#!/usr/bin/env bash
# claude-bridge bug reporter (MCP edition)
#
# 현장 증거를 bugreport/YYYYMMDD_HHMMSS/ 에 수집한 뒤, 해당 디렉토리에서
# 대화형 Claude Code 를 띄워 증상 청취 → 분석 → BUG_REPORT.md 생성.
# 실제 코드 수정은 하지 않음 (수정은 ./bin/repairer.sh).

set -euo pipefail

cd "$(dirname "$0")/.."

CB_HOME="${CB_HOME:-$HOME/.claude-bridge}"
CONFIG_PATH="$CB_HOME/config.json"
ANOMALY_LOG="$CB_HOME/anomaly.jsonl"
LOG_DIR="$CB_HOME/logs"

TS=$(date +%Y%m%d_%H%M%S)
REPORT_DIR="bugreport/$TS"
mkdir -p "$REPORT_DIR"

TMUX_BIN=$(command -v tmux || echo "/opt/homebrew/bin/tmux")

printf '========================================\n'
printf '  claude-bridge bugreporter\n'
printf '  ID: %s\n' "$TS"
printf '========================================\n\n'

# 1. tmux snapshot (all cb-* sessions)
echo "[1/6] tmux panes..."
{
  echo "# tmux sessions"
  "$TMUX_BIN" list-sessions 2>&1 || echo "(no tmux server)"
  echo
  mapfile -t CB_SESSIONS < <("$TMUX_BIN" list-sessions -F "#{session_name}" 2>/dev/null | grep '^cb-' || true)
  for s in "${CB_SESSIONS[@]}"; do
    echo "# === pane capture: $s (last 500) ==="
    "$TMUX_BIN" capture-pane -t "$s" -p -S -500 2>&1 || echo "(capture failed)"
    echo
    echo "# pane flags: $s"
    "$TMUX_BIN" list-panes -t "$s" -F "pane_dead=#{pane_dead} pane_pid=#{pane_pid}" 2>&1 || true
    echo
  done
} > "$REPORT_DIR/tmux_capture.txt"

# 2. anomaly log
echo "[2/6] anomaly log..."
if [ -f "$ANOMALY_LOG" ]; then
  cp "$ANOMALY_LOG" "$REPORT_DIR/anomaly.jsonl" 2>/dev/null || true
  for suffix in .1 .2 .3; do
    [ -f "${ANOMALY_LOG}${suffix}" ] && cp "${ANOMALY_LOG}${suffix}" "$REPORT_DIR/anomaly.jsonl${suffix}" 2>/dev/null || true
  done
fi

# 3. dispatcher logs
echo "[3/6] dispatcher logs..."
if [ -d "$LOG_DIR" ]; then
  mkdir -p "$REPORT_DIR/logs"
  # last 3 days
  find "$LOG_DIR" -maxdepth 1 -name "*.log" -type f -mtime -3 -exec cp {} "$REPORT_DIR/logs/" \; 2>/dev/null || true
fi

# 4. config (masked)
echo "[4/6] config (token masked)..."
if [ -f "$CONFIG_PATH" ]; then
  # mask botToken — keep first 10 chars for disambiguation, hide the rest
  bun -e '
    const fs = require("fs");
    const raw = fs.readFileSync(process.argv[1], "utf-8");
    const obj = JSON.parse(raw);
    if (obj.botToken) obj.botToken = obj.botToken.slice(0, 10) + "…MASKED…";
    process.stdout.write(JSON.stringify(obj, null, 2));
  ' "$CONFIG_PATH" > "$REPORT_DIR/config_masked.json" 2>/dev/null || cp "$CONFIG_PATH" "$REPORT_DIR/config_RAW_REVIEW_BEFORE_SHARING.json"
fi

# 5. source snapshot + git
echo "[5/6] source + git state..."
mkdir -p "$REPORT_DIR/src"
cp -R src/core "$REPORT_DIR/src/" 2>/dev/null || true
cp -R src/channels "$REPORT_DIR/src/" 2>/dev/null || true
cp src/dispatcher.ts "$REPORT_DIR/src/" 2>/dev/null || true
{
  echo "# git log (recent 20)"
  git log --oneline -20 2>/dev/null || echo "(not a git repo)"
  echo
  echo "# git status"
  git status --short 2>/dev/null || true
  echo
  echo "# git diff --stat"
  git diff --stat 2>/dev/null || true
} > "$REPORT_DIR/git_state.txt"

# 6. CLAUDE.md guide
echo "[6/6] writing CLAUDE.md guide..."
cat > "$REPORT_DIR/CLAUDE.md" <<EOF
# claude-bridge bug report session — ${TS}

- created: $(date '+%Y-%m-%d %H:%M:%S')

## your role

You are diagnosing claude-bridge (Bun/TS MCP channel host). Your job in this
session is **evidence analysis + root-cause hypothesis + proposed fix**.
**Do not edit project source files here.** Writing is done by
\`./bin/repairer.sh\` in a separate session that reads the
\`BUG_REPORT.md\` you produce here.

First action: ask the user "what went wrong, and roughly when?"

## evidence in this directory

| file | content |
|------|---------|
| \`tmux_capture.txt\` | every \`cb-*\` tmux session's pane tail + flags |
| \`anomaly.jsonl\` (+\`.1/.2/.3\`) | always-on anomaly log (rotated) |
| \`logs/\` | dispatcher stdout/stderr (last 3 days) |
| \`config_masked.json\` | config with botToken masked |
| \`src/\` | source snapshot at time of report |
| \`git_state.txt\` | recent commits + diff stat |

## workflow

1. **symptom intake** — ask the user what happened and when (timestamps).
2. **read evidence** — Read every file above. Cross-reference anomaly.jsonl
   timestamps against tmux capture lines and log entries.
3. **timeline reconstruction** — build a table: ts | source | event.
4. **ask for gaps** — request screenshots, reproduction steps, whatever is
   missing.
5. **write BUG_REPORT.md** in this directory. Never edit project files.

## BUG_REPORT.md template

\`\`\`markdown
# bug report — ${TS}

## symptom
(1-2 sentences)

## timeline
| ts | source | event |
|----|--------|-------|
| HH:MM:SS | anomaly | channel_reply_failed { reason: ... } |
| HH:MM:SS | tmux    | pane shows "..." |
| HH:MM:SS | log     | dispatcher: "..." |

## hypotheses
1. **A** — evidence: anomaly L12, tmux L8-10
2. **B** — evidence: ...

## reproduction
1. ...

## proposed fix
(actual file edits are NOT done here. the diff below is a proposal.
 repairer.sh will apply it in a separate session.)

### src/path/to/file.ts
\`\`\`diff
- old line
+ new line
\`\`\`

### risks / side-effects
...

### tests
- [ ] add tests/... covering ...

## missing info
- [ ] ...
\`\`\`

---

Start by asking: "무슨 일이 있었나요? 시각과 함께 알려주세요."
EOF

echo
printf '========================================\n'
printf '  evidence collected\n'
printf '========================================\n'
echo "  dir: $REPORT_DIR/"
ls -la "$REPORT_DIR/"
echo

echo "starting Claude Code in $REPORT_DIR ..."
echo "(CLAUDE.md will auto-load. bot source is NOT writable from this cwd.)"
echo
cd "$REPORT_DIR"
exec claude "Read CLAUDE.md first, then ask me '무슨 일이 있었나요? 시각과 함께 알려주세요.'"
