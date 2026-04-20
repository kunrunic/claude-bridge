#!/usr/bin/env bash
# claude-bridge initial setup (MCP edition)
#
# 대화형 스크립트:
#  1) bun / tmux / claude CLI 확인
#  2) bun install
#  3) Bot Token 입력 (masked)
#  4) Telegram user_id / chat_id 입력 또는 @userinfobot 안내
#  5) ~/.claude-bridge/config.json 생성 + 권한 0600

set -euo pipefail

cd "$(dirname "$0")/.."

CB_HOME="${CB_HOME:-$HOME/.claude-bridge}"
CONFIG_PATH="$CB_HOME/config.json"

printf '========================================\n'
printf '  claude-bridge setup\n'
printf '========================================\n\n'

# 1. bun
if ! command -v bun >/dev/null 2>&1; then
  echo "✗ bun not found. install from https://bun.sh then re-run." >&2
  exit 1
fi
echo "✓ bun $(bun --version)"

# 2. tmux
if ! command -v tmux >/dev/null 2>&1; then
  echo "✗ tmux not found. macOS: brew install tmux / linux: apt install tmux" >&2
  exit 1
fi
echo "✓ tmux $(tmux -V | awk '{print $2}')"

# 3. claude CLI
if ! command -v claude >/dev/null 2>&1; then
  echo "✗ 'claude' CLI not found on PATH." >&2
  echo "  install Claude Code CLI first: https://claude.com/product/claude-code" >&2
  exit 1
fi
echo "✓ claude $(claude --version 2>/dev/null | head -1 || echo '(version unknown)')"

# 4. bun install
if [ ! -d node_modules ]; then
  echo
  echo "running: bun install"
  bun install
else
  echo "✓ node_modules present (skip bun install — run manually if package.json changed)"
fi

# 5. ~/.claude-bridge/
mkdir -p "$CB_HOME" "$CB_HOME/telegram" "$CB_HOME/workspaces" "$CB_HOME/logs"
chmod 700 "$CB_HOME"

# 6. existing config?
if [ -f "$CONFIG_PATH" ]; then
  echo
  echo "⚠️  existing config: $CONFIG_PATH"
  read -r -p "overwrite? [y/N] " yn
  case "${yn:-N}" in
    [Yy]*) ;;
    *) echo "keeping existing config. done."; exit 0 ;;
  esac
fi

# 7. bot token (masked)
echo
echo "── Telegram Bot ─────────────────────────"
echo "paste bot token from @BotFather (input is masked):"
printf "Bot Token: "
BOT_TOKEN=""
while IFS= read -r -s -n 1 ch; do
  [ -z "$ch" ] && break
  if [ "$ch" = $'\x7f' ]; then
    if [ -n "$BOT_TOKEN" ]; then
      BOT_TOKEN="${BOT_TOKEN%?}"
      printf '\b \b'
    fi
    continue
  fi
  BOT_TOKEN+="$ch"
  printf '*'
done
echo
echo "(${#BOT_TOKEN} chars entered, prefix: ${BOT_TOKEN:0:10}...)"
if [ ${#BOT_TOKEN} -lt 20 ]; then
  echo "✗ token looks too short" >&2
  exit 1
fi

# 8. allowlist entry
echo
echo "── Allowlist ────────────────────────────"
cat <<'INFO'
enter your Telegram numeric user_id (required).
how to find it:
  · message @userinfobot on Telegram, it replies with your ID
  · or open https://t.me/your_bot_username in a browser — URL contains ids after first interaction

the id is an integer like 123456789. it goes into config "allowlist" as a string.
INFO
printf 'your user_id: '
read -r USER_ID
if ! [[ "$USER_ID" =~ ^[0-9]+$ ]]; then
  echo "✗ not a number" >&2
  exit 1
fi

# 9. write config
cat > "$CONFIG_PATH" <<EOF
{
  "botToken": "$BOT_TOKEN",
  "allowlist": ["$USER_ID"],
  "defaultChatId": "$USER_ID",
  "dumpEnabled": false
}
EOF
chmod 600 "$CONFIG_PATH"

echo
printf '========================================\n'
printf '  setup complete\n'
printf '========================================\n'
echo "  config:  $CONFIG_PATH"
echo "  start:   ./bin/start.sh"
echo "  stop:    ./bin/stop.sh"
