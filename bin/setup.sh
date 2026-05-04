#!/usr/bin/env bash
# claude-bridge initial setup (MCP edition)
#
# 대화형 스크립트. 두 모드 지원:
#  · Telegram + CLI: 모바일에서 Telegram bot 으로 + 원격 SSH 에서 cb CLI 로 동시 사용
#  · CLI only      : 원격 SSH 에서 cb CLI 만 사용 (Telegram bot 미사용)
#
# Idempotent — 재실행 시 누락된 부분만 채운다:
#  · config 가 이미 있고 "overwrite? N" 답하면 토큰/유저ID 다시 안 묻고 후속 단계만 갱신
#  · MCP 등록 / slash / cb 심링크 모두 이미 있으면 skip
#  · 옛 tg_channel 등록 잔재 발견 시 정리 + bridge-channel 재등록

set -euo pipefail

cd "$(dirname "$0")/.."

CB_HOME="${CB_HOME:-$HOME/.claude-bridge}"
CONFIG_PATH="$CB_HOME/config.json"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVER_ABS="$REPO_ROOT/src/channels/telegram/server.ts"

printf '========================================\n'
printf '  claude-bridge setup\n'
printf '========================================\n\n'

# ── 1. dependencies ─────────────────────────────────────────────────────────

if ! command -v bun >/dev/null 2>&1; then
  echo "✗ bun not found."
  read -r -p "  지금 설치할까요? [y/N] " yn
  case "${yn:-N}" in
    [Yy]*)
      echo "installing bun..."
      curl -fsSL https://bun.sh/install | bash
      export PATH="$HOME/.bun/bin:$PATH"
      if ! command -v bun >/dev/null 2>&1; then
        echo "✗ 설치 후에도 bun을 찾을 수 없습니다. 새 터미널을 열고 다시 실행하세요." >&2
        exit 1
      fi
      echo "✓ bun 설치 완료"
      ;;
    *)
      echo "  수동 설치: curl -fsSL https://bun.sh/install | bash" >&2
      exit 1
      ;;
  esac
fi
echo "✓ bun $(bun --version)"

if ! command -v tmux >/dev/null 2>&1; then
  echo "✗ tmux not found. macOS: brew install tmux / linux: apt install tmux" >&2
  exit 1
fi
echo "✓ tmux $(tmux -V | awk '{print $2}')"

if ! command -v claude >/dev/null 2>&1; then
  echo "✗ 'claude' CLI not found on PATH." >&2
  echo "  install Claude Code CLI first: https://claude.com/product/claude-code" >&2
  exit 1
fi
echo "✓ claude $(claude --version 2>/dev/null | head -1 || echo '(version unknown)')"

# ── 2. bun install ──────────────────────────────────────────────────────────

if [ ! -d node_modules ]; then
  echo
  echo "running: bun install"
  bun install
else
  echo "✓ node_modules present (skip bun install — run manually if package.json changed)"
fi

# ── 3. CB_HOME ──────────────────────────────────────────────────────────────

mkdir -p "$CB_HOME" "$CB_HOME/telegram" "$CB_HOME/workspaces" "$CB_HOME/logs"
chmod 700 "$CB_HOME"

# ── 4. existing config? config 작성과 후속 단계 분리 ─────────────────────────

SKIP_CONFIG_WRITE=0
if [ -f "$CONFIG_PATH" ]; then
  echo
  echo "⚠️  existing config: $CONFIG_PATH"
  read -r -p "overwrite config? [y/N] " yn
  case "${yn:-N}" in
    [Yy]*) ;;
    *)
      SKIP_CONFIG_WRITE=1
      echo "  → 기존 config 유지. 후속 단계 (MCP / slash / cb 심링크) 만 갱신합니다."
      ;;
  esac
fi

# ── 5. mode 결정 — config 유지 시 botToken 유무로 추론, 새로 작성 시 prompt ────

if [ "$SKIP_CONFIG_WRITE" = "1" ]; then
  if command -v jq >/dev/null 2>&1; then
    HAS_TOKEN=$(jq -r 'has("botToken") and (.botToken | length > 0)' "$CONFIG_PATH" 2>/dev/null || echo "false")
  else
    HAS_TOKEN="false"
    grep -q '"botToken"[[:space:]]*:[[:space:]]*"[^"]*"' "$CONFIG_PATH" 2>/dev/null && HAS_TOKEN="true"
  fi
  if [ "$HAS_TOKEN" = "true" ]; then
    MODE="telegram-cli"
    echo "  → 기존 config: Telegram + CLI 모드"
  else
    MODE="cli-only"
    echo "  → 기존 config: CLI only 모드"
  fi
else
  echo
  echo "── Setup mode ───────────────────────────"
  cat <<'MODE'
1) Telegram + CLI  : 모바일은 Telegram bot, 원격 SSH 는 cb CLI (둘 다)
2) CLI only        : 원격 SSH 에서 cb CLI 로만 사용 (Telegram bot 안 씀)
MODE
  read -r -p "Choose [1]: " MODE_INPUT
  MODE_INPUT="${MODE_INPUT:-1}"
  case "$MODE_INPUT" in
    1) MODE="telegram-cli" ;;
    2) MODE="cli-only" ;;
    *) echo "✗ invalid choice: $MODE_INPUT" >&2; exit 1 ;;
  esac
fi

# ── 6. config 작성 (SKIP_CONFIG_WRITE=0 일 때만) ─────────────────────────────

if [ "$SKIP_CONFIG_WRITE" = "0" ]; then
  if [ "$MODE" = "telegram-cli" ]; then
    # bot token (masked)
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

    # allowlist entry
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

    # write Telegram + CLI config
    cat > "$CONFIG_PATH" <<EOF
{
  "botToken": "$BOT_TOKEN",
  "allowlist": ["$USER_ID"],
  "defaultChatId": "$USER_ID",
  "dumpEnabled": false
}
EOF
    chmod 600 "$CONFIG_PATH"
    echo "✓ config written: Telegram + CLI 모드"
  else
    # CLI only — 최소 config
    cat > "$CONFIG_PATH" <<EOF
{
  "skipPermissions": false,
  "dumpEnabled": false
}
EOF
    chmod 600 "$CONFIG_PATH"
    echo "✓ config written: CLI only 모드 (Telegram 미사용)"
  fi
fi

# ── 7. 후속 단계: bridge-channel MCP user-scope 등록 (Telegram 모드에만 의미) ───

if [ "$MODE" = "telegram-cli" ]; then
  echo
  echo "── bridge-channel MCP (user scope) ──────"

  # NOTE: `claude mcp get` 은 project/local/user scope 를 모두 찾는다.
  # 프로젝트 scope (.mcp.json) 로 등록되어 있으면 cwd 가 claude-bridge 밖일 때
  # 안 보이므로, /tmp 에서 실행해 프로젝트 scope 를 배제한 뒤 확인.

  # 옛 이름 (tg_channel) 잔재 감지 → 정리
  if ( cd /tmp && claude mcp get tg_channel ) >/dev/null 2>&1; then
    echo "  옛 'tg_channel' 등록 발견 — 'bridge-channel' 로 갱신합니다"
    if claude mcp remove -s user tg_channel >/dev/null 2>&1; then
      echo "  ✓ tg_channel 제거"
    else
      echo "  ⚠️  tg_channel 자동 제거 실패. 수동으로:"
      echo "     claude mcp remove -s user tg_channel"
    fi
  fi

  if ( cd /tmp && claude mcp get bridge-channel ) >/dev/null 2>&1; then
    echo "✓ bridge-channel 이미 user scope 에 등록됨"
  else
    if claude mcp add -s user bridge-channel bun "$SERVER_ABS" >/dev/null 2>&1; then
      echo "✓ bridge-channel user-scope 등록 완료"
    else
      echo "⚠️  자동 등록 실패. 수동으로:"
      echo "     claude mcp add -s user bridge-channel bun \"$SERVER_ABS\""
    fi
  fi

  # ── 8. /handoff-to-bridge slash command (덮어쓰기 — idempotent) ─────────────

  echo
  echo "── /handoff-to-bridge slash command ─────"
  HANDOFF_SCRIPT_ABS="$REPO_ROOT/bin/handoff-to-bridge.sh"
  HANDOFF_TEMPLATE="$REPO_ROOT/bin/handoff-to-bridge.md.tpl"
  USER_COMMANDS_DIR="$HOME/.claude/commands"
  USER_COMMAND_FILE="$USER_COMMANDS_DIR/handoff-to-bridge.md"
  if [ ! -f "$HANDOFF_TEMPLATE" ]; then
    echo "✗ template missing: $HANDOFF_TEMPLATE" >&2
    exit 1
  fi
  mkdir -p "$USER_COMMANDS_DIR"
  sed "s|__HANDOFF_SCRIPT_ABS__|$HANDOFF_SCRIPT_ABS|g" "$HANDOFF_TEMPLATE" > "$USER_COMMAND_FILE"
  echo "✓ $USER_COMMAND_FILE"
fi

# ── 9. cb / cb-tui 글로벌 심링크 ─────────────────────────────────

echo
echo "── Global cb commands (~/.local/bin) ────"
LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"

# cb (클라이언트 진입점), cb-tui (deprecated stub — cb-menu attach 로 redirect).
# 메뉴 UI 는 dispatcher 가 띄우는 cb-menu tmux session 안의 ink TUI 가 담당.
# popup helper(cb-helper) 는 더 이상 쓰지 않으므로 제거.

# 이전 버전 setup 이 남긴 cb-helper 심링크 정리.
if [ -L "$LOCAL_BIN/cb-helper" ] || [ -f "$LOCAL_BIN/cb-helper" ]; then
  rm -f "$LOCAL_BIN/cb-helper"
  echo "  ✓ removed stale $LOCAL_BIN/cb-helper"
fi

for binname in cb cb-tui; do
  TARGET="$REPO_ROOT/bin/$binname"
  LINK="$LOCAL_BIN/$binname"
  if [ ! -f "$TARGET" ]; then
    echo "  ⚠️  $TARGET 없음 (skip)"
    continue
  fi
  if [ -L "$LINK" ] || [ -f "$LINK" ]; then
    CURRENT="$(readlink "$LINK" 2>/dev/null || echo "")"
    if [ "$CURRENT" = "$TARGET" ]; then
      echo "  ✓ $LINK → $TARGET (이미 정확)"
    else
      rm -f "$LINK"
      ln -s "$TARGET" "$LINK"
      echo "  ✓ $LINK → $TARGET (갱신)"
    fi
  else
    ln -s "$TARGET" "$LINK"
    echo "  ✓ $LINK → $TARGET"
  fi
done

case ":$PATH:" in
  *":$LOCAL_BIN:"*) echo "  PATH 이미 포함" ;;
  *) echo "  ⚠️  PATH 에 $LOCAL_BIN 없음. shell rc 에 추가:"
     echo "     export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# cb connect 는 ssh 가 직접 `tmux attach -t cb-menu` 를 호출하므로 PATH 의존성이
# 줄었지만, dispatcher 가 spawn 하는 cb-menu 안에서 bun 을 실행하려면 PATH 에
# bun 이 있어야 한다. ssh non-interactive 에서도 bun/tmux 발견되도록 ~/.zshenv 셋업.
ZSHENV="$HOME/.zshenv"
if [ ! -f "$ZSHENV" ]; then
  read -r -p "  ~/.zshenv 에 PATH 추가? (ssh 비대화형 / dispatcher spawn 용 bun/tmux 경로) [Y/n] " yn
  case "${yn:-Y}" in
    [Nn]*) echo "  (건너뜀 — ssh 안 PATH 별도 셋업 필요)" ;;
    *)
      cat > "$ZSHENV" <<'EOF'
# Non-interactive shells (ssh remote commands, scripts) need explicit PATH —
# .zshrc only runs for interactive shells. dispatcher 가 cb-menu 를 spawn 할 때,
# 그리고 ssh remote command 에서 tmux 를 호출할 때 모두 이 PATH 가 필요하다.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$HOME/.bun/bin:$PATH"
EOF
      echo "  ✓ ~/.zshenv 생성"
      ;;
  esac
fi

# ── 10. cb localhost 자동 등록 ──────────────────────────────────────────────
# 셋업 호스트에서 즉시 `cb localhost` 로 cb-menu 진입 가능하도록 기본 등록.
# 이미 있으면 건너뜀. SSH 키/sshd 설정은 사용자 책임 (실패 시 메시지로 안내).

CB_BIN="$LOCAL_BIN/cb"
if [ -x "$CB_BIN" ] || [ -L "$CB_BIN" ]; then
  echo
  echo "── cb localhost 등록 ────────────────────"
  if "$CB_BIN" list 2>/dev/null | awk 'NR>2 {print $1}' | grep -qx "localhost"; then
    echo "✓ 이미 등록됨"
  else
    if "$CB_BIN" add localhost \
        --host=localhost \
        --port=22 \
        --user="$USER" \
        --key="" </dev/null >/dev/null 2>&1; then
      echo "✓ cb localhost 등록 — 같은 머신에서 'cb localhost' 로 진입 가능"
      echo "  (sshd 가 켜져 있고 ~/.ssh/authorized_keys 에 키 등록 필요)"
    else
      echo "⚠️  cb localhost 등록 실패 — 수동 등록:"
      echo "    cb add localhost --host=localhost --user=\"\$USER\""
    fi
  fi
fi

# ── 11. 완료 ────────────────────────────────────────────────────────────────

echo
printf '========================================\n'
printf '  setup complete\n'
printf '========================================\n'
echo "  mode:    $MODE"
echo "  config:  $CONFIG_PATH"
if [ "$MODE" = "telegram-cli" ]; then
  echo "  server:  $SERVER_ABS (registered at user scope)"
  echo "  slash:   /handoff-to-bridge (user scope)"
fi
echo "  start:   ./bin/start.sh"
echo "  stop:    ./bin/stop.sh"
echo "  cli:     cb help   (또는 ./bin/cb help)"
