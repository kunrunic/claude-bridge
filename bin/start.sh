#!/usr/bin/env bash
# claude-bridge start (MCP edition)
#
# 기본: 백그라운드 기동, 로그는 ~/.claude-bridge/logs/YYYY-MM-DD.log
# --fg: 전경 실행 (Ctrl+C 로 종료)

set -euo pipefail

cd "$(dirname "$0")/.."

# bun 이 비인터랙티브 SSH 세션에서 PATH 에 없을 수 있음 — 공통 위치 탐색
if ! command -v bun >/dev/null 2>&1; then
  for _d in "$HOME/.bun/bin" "/usr/local/bin" "$HOME/.local/bin"; do
    [ -x "$_d/bun" ] && export PATH="$_d:$PATH" && break
  done
fi

CB_HOME="${CB_HOME:-$HOME/.claude-bridge}"
CONFIG_PATH="$CB_HOME/config.json"
# dispatcher process 추적용 PID 파일 — 모드(bridge/native)와 무관.
# Telegram polling lock 의 PID (CB_HOME/telegram/bot.pid) 와는 책임이 다르다:
#  · dispatcher.pid : start.sh / stop.sh 가 dispatcher 프로세스 자체를 추적
#  · telegram/bot.pid : TelegramChannel.acquirePollingLock 이 같은 봇 토큰
#                       중복 폴링 방지용 (bridge 모드에서만 생성됨)
PID_FILE="$CB_HOME/dispatcher.pid"
LOG_DIR="$CB_HOME/logs"

FOREGROUND=0
for arg in "$@"; do
  case "$arg" in
    --fg|--foreground) FOREGROUND=1 ;;
    -h|--help)
      cat <<'USAGE'
usage: ./bin/start.sh [--fg]
  --fg   run in foreground (Ctrl+C to stop). default is background.
logs at ~/.claude-bridge/logs/YYYY-MM-DD.log
USAGE
      exit 0
      ;;
    *)
      echo "unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

# config present?
if [ ! -f "$CONFIG_PATH" ]; then
  echo "✗ config not found: $CONFIG_PATH" >&2
  echo "  run ./bin/setup.sh first" >&2
  exit 1
fi

# already running?
if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    echo "already running (PID=$PID). stop with ./bin/stop.sh" >&2
    exit 1
  fi
  rm -f "$PID_FILE"
fi

mkdir -p "$CB_HOME" "$LOG_DIR"
# prune logs > 7 days
find "$LOG_DIR" -maxdepth 1 -name "*.log" -type f -mtime +7 -delete 2>/dev/null || true

# Shift+Enter 등 extended key sequence 가 SSH 재접속 후에도 동작하도록.
# tmux 3.3+ 에서 지원. 구버전은 무시.
# client-attached 훅: 재접속 시마다 refresh-client 로 extended key 재협상 강제.
tmux set -g extended-keys on 2>/dev/null || true
tmux set-hook -g client-attached "refresh-client" 2>/dev/null || true

# scrollback / copy-mode UX.
# mouse on — wheel 이벤트를 tmux 가 캡처해 copy-mode 진입 + 스크롤. tmux 가 pane 을
# scroll-region 으로 redraw 하는 구조라 alt-screen 을 비활성화해도 터미널 native
# scrollback 에는 안 쌓임. 결국 wheel→copy-mode 가 모든 터미널에서 통일된 정답.
# 부작용: 텍스트 선택이 tmux 마우스 모드로 가지만 iTerm/wezterm/kitty 등은 Option(⌥)
# 누르고 드래그하면 native select 가능 — 일반적인 tmux+iTerm 워크플로우.
tmux set -g  history-limit 50000 2>/dev/null || true
tmux set -g  mouse on 2>/dev/null || true
tmux set -as terminal-features "*:extkeys" 2>/dev/null || true
tmux set -wg mode-keys vi 2>/dev/null || true

LOG_FILE="$LOG_DIR/$(date +%Y-%m-%d).log"

if [ "$FOREGROUND" = "1" ]; then
  exec bun run src/dispatcher.ts
fi

# background — mdwiz/PTY 안전: 새 세션으로 분리해 제어터미널 SIGHUP 을 차단.
# start.sh 를 '명령 단위 PTY'(예: mdwiz shell_run) 에서 돌려도 dispatcher 가
# hangup 으로 죽지 않도록 setsid 로 세션 리더화하고 stdin 을 /dev/null 로 뗀다.
# macOS 엔 setsid 명령이 없어 perl(POSIX::setsid) 로 처리하며, fork 후 자식에서
# setsid 를 호출해야 EPERM(그룹 리더) 을 피한다. exec 로 PID 가 유지되므로
# 부모가 출력하는 자식 PID 가 곧 dispatcher(bun) 의 PID 다.
if command -v perl >/dev/null 2>&1; then
  BOT_PID=$(perl -e '
    my $pid = fork;
    die "fork: $!\n" unless defined $pid;
    if ($pid) { print "$pid\n"; exit 0; }
    require POSIX; POSIX::setsid() or die "setsid: $!\n";
    exec(@ARGV) or die "exec: $!\n";
  ' -- bash -c "exec bun run src/dispatcher.ts </dev/null >> '$LOG_FILE' 2>&1")
else
  # perl 없는 환경 fallback — 명령단위 PTY 에선 SIGHUP 으로 죽을 수 있음.
  echo "  ⚠️  perl 없음 — nohup fallback (제어터미널 종료 시 SIGHUP 위험)" >&2
  nohup bun run src/dispatcher.ts </dev/null >> "$LOG_FILE" 2>&1 &
  BOT_PID=$!
  disown "$BOT_PID" 2>/dev/null || true
fi

if [ -z "${BOT_PID:-}" ]; then
  echo "✗ failed to launch — see $LOG_FILE" >&2
  exit 1
fi
echo "$BOT_PID" > "$PID_FILE"

sleep 1
if kill -0 "$BOT_PID" 2>/dev/null; then
  echo "✓ started (PID=$BOT_PID)"
  echo "  log:  $LOG_FILE"
  echo "  stop: ./bin/stop.sh"
else
  echo "✗ failed to start — see $LOG_FILE" >&2
  rm -f "$PID_FILE"
  exit 1
fi
