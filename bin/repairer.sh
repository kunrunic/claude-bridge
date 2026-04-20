#!/usr/bin/env bash
# claude-bridge repair tool (MCP edition)
#
# bugreport/<TS>/BUG_REPORT.md 의 제안 수정안을 프로젝트 루트에서 실제 코드에 반영.
# 세션 종료 시 임시 CLAUDE.md 는 원복.
#
# 사용:
#   ./bin/repairer.sh                         # 가장 최근 bugreport
#   ./bin/repairer.sh bugreport/20260420_...  # 특정 리포트

set -euo pipefail

cd "$(dirname "$0")/.."

if [ $# -ge 1 ]; then
  REPORT_DIR="$1"
else
  REPORT_DIR=$(find bugreport -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | tail -1 || true)
fi

if [ -z "${REPORT_DIR:-}" ] || [ ! -d "$REPORT_DIR" ]; then
  echo "✗ no bugreport directory. run ./bin/bugreporter.sh first." >&2
  exit 1
fi

REPORT_MD="$REPORT_DIR/BUG_REPORT.md"
if [ ! -f "$REPORT_MD" ]; then
  echo "✗ $REPORT_MD not found." >&2
  echo "  the bugreporter session must produce BUG_REPORT.md before repairer runs." >&2
  exit 1
fi

TS=$(date +%Y%m%d_%H%M%S)
REPORT_ABS=$(cd "$REPORT_DIR" && pwd)

printf '========================================\n'
printf '  claude-bridge repairer\n'
printf '  report: %s\n' "$REPORT_DIR"
printf '  started: %s\n' "$TS"
printf '========================================\n\n'

# uncommitted changes warn
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
  echo "⚠️  working tree has uncommitted changes. consider commit/stash first."
  read -r -p "continue anyway? [y/N] " yn
  case "${yn:-N}" in
    [Yy]*) ;;
    *) echo "aborted."; exit 1 ;;
  esac
fi

# temp CLAUDE.md (backup existing)
PROJECT_CLAUDE_MD="CLAUDE.md"
BACKUP=""
if [ -f "$PROJECT_CLAUDE_MD" ]; then
  BACKUP="${PROJECT_CLAUDE_MD}.repairer_backup_${TS}"
  cp "$PROJECT_CLAUDE_MD" "$BACKUP"
fi

cat > "$PROJECT_CLAUDE_MD" <<EOF
# claude-bridge repair session — ${TS}

- report: ${REPORT_ABS}/BUG_REPORT.md

## your role

Apply the **proposed fix** section of the report above to the real source files
in this repo. You are in the project root with write access.

## workflow

1. **read the report** — \`Read ${REPORT_ABS}/BUG_REPORT.md\` first.
   understand symptom / hypotheses / proposed diff.
2. **sanity check** — file paths/function names may have drifted since the
   report was written. grep the current tree before editing.
3. **summarize scope** — before any Edit, print a bullet list: files to
   change + one-line intent each. ask the user "적용할까요?" and wait.
4. **apply edits** — Edit tool only. one file at a time: Read → understand
   → Edit. never bulk-edit blind.
5. **run tests** — \`bun run typecheck\` then \`bun test\`. if a test fails,
   explain the failure and stop; do **not** force-pass by relaxing the test.
6. **propose commit message** — do NOT commit. print a draft commit message
   for the user to copy.

## rules

- **no Co-Authored-By trailer.** this project forbids Claude/Anthropic
  co-author attribution in commits.
- no scope creep: only changes mentioned in the report. no drive-by
  refactors, no formatting sweeps.
- if the fix is wrong for current code, say so — do not try to make it work
  by editing unrelated files.

## first action

Read ${REPORT_ABS}/BUG_REPORT.md, then print the scope summary and ask for
approval before editing anything.
EOF

echo "✓ temp CLAUDE.md written"
[ -n "$BACKUP" ] && echo "  (existing CLAUDE.md backed up to $BACKUP)"
echo

restore_claude_md() {
  if [ -n "$BACKUP" ] && [ -f "$BACKUP" ]; then
    mv "$BACKUP" "$PROJECT_CLAUDE_MD"
    echo "✓ CLAUDE.md restored from backup"
  else
    rm -f "$PROJECT_CLAUDE_MD"
    echo "✓ temp CLAUDE.md removed"
  fi
}
trap restore_claude_md EXIT INT TERM

echo "starting Claude Code at project root..."
echo "(editing allowed. temp CLAUDE.md reverts on exit.)"
echo
claude "Read CLAUDE.md, then ${REPORT_ABS}/BUG_REPORT.md. Summarize the fix scope and ask me '적용할까요?'"
