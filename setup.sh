#!/bin/bash
# claude-bridge 초기 설치 스크립트
set -e

cd "$(dirname "$0")"

echo "========================================"
echo "  claude-bridge 설치"
echo "========================================"
echo

# ── 1. Python 3.11+ 확인 ─────────────────────────────────────
PYTHON=""
for cmd in python3.11 python3.12 python3.13 python3; do
    if command -v $cmd >/dev/null 2>&1; then
        ver=$($cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=$(echo $ver | cut -d. -f1)
        minor=$(echo $ver | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON=$cmd
            echo "✓ Python $ver ($cmd)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "✗ Python 3.11+ 필요. 다음 중 하나로 설치하세요:"
    echo "  brew install python@3.11"
    echo "  pyenv install 3.11.14"
    exit 1
fi

# ── 2. tmux 확인 ─────────────────────────────────────────────
if ! command -v tmux >/dev/null 2>&1; then
    echo "✗ tmux 없음. brew install tmux 실행하세요."
    exit 1
fi
echo "✓ tmux $(tmux -V | awk '{print $2}')"

# ── 3. venv 생성 ─────────────────────────────────────────────
if [ ! -d "venv" ]; then
    echo
    read -p "venv 를 생성할까요? [Y/n] " yn
    yn=${yn:-Y}
    if [[ "$yn" =~ ^[Yy]$ ]]; then
        $PYTHON -m venv venv
        echo "✓ venv 생성됨"
    else
        echo "✗ venv 없이는 진행 불가"
        exit 1
    fi
fi

source venv/bin/activate

# ── 4. 패키지 설치 ────────────────────────────────────────────
if ! python -c "import telegram" 2>/dev/null; then
    echo
    read -p "python-telegram-bot 을 설치할까요? [Y/n] " yn
    yn=${yn:-Y}
    if [[ "$yn" =~ ^[Yy]$ ]]; then
        pip install --upgrade pip >/dev/null
        pip install python-telegram-bot
        echo "✓ 패키지 설치 완료"
    else
        echo "✗ 패키지 없이는 진행 불가"
        exit 1
    fi
else
    echo "✓ python-telegram-bot 이미 설치됨"
fi

# ── 5. Claude Code CLI 경로 ──────────────────────────────────
CLAUDE_PATH="/Applications/cmux.app/Contents/Resources/bin/claude"
if [ ! -x "$CLAUDE_PATH" ]; then
    CLAUDE_PATH=$(which claude 2>/dev/null || echo "")
    if [ -z "$CLAUDE_PATH" ]; then
        read -p "claude 실행 경로 입력: " CLAUDE_PATH
    fi
fi
echo "✓ claude: $CLAUDE_PATH"

# ── 6. 봇 토큰 입력 (블러 처리) ────────────────────────────────
echo
echo "── Telegram Bot 설정 ────────────────────────"
echo "@BotFather 에서 발급받은 토큰을 입력하세요."
stty -echo
printf "Bot Token: "
read BOT_TOKEN
stty echo
echo "(입력됨: ${BOT_TOKEN:0:10}****...)"

if [ -z "$BOT_TOKEN" ]; then
    echo "✗ 토큰 필수"
    exit 1
fi

# ── 7. config.json 임시 작성 ─────────────────────────────────
# 폴더 이름 기반 tmux 세션명 자동 설정 (여러 인스턴스 동시 운영 대응)
TMUX_NAME=$(basename "$PWD" | tr '-' '_')

cat > config.json <<EOF
{
  "token": "$BOT_TOKEN",
  "allowed_ids": [],
  "tmux_session": "$TMUX_NAME",
  "claude_path": "$CLAUDE_PATH"
}
EOF
echo "✓ tmux 세션명: $TMUX_NAME"

# ── 8. 봇 일시 실행 → /whoami 로 chat_id 수집 ──────────────────
echo
echo "── chat_id 수집 ────────────────────────────"
echo "1) 텔레그램에서 봇을 연 뒤 /whoami 를 입력하세요."
echo "2) 봇이 응답한 chat_id 숫자를 여기 입력하면 됩니다."
echo

mkdir -p logs
python bot.py > logs/setup.log 2>&1 &
BOT_PID=$!
sleep 2

if ! kill -0 $BOT_PID 2>/dev/null; then
    echo "✗ 봇 실행 실패. logs/setup.log 확인"
    exit 1
fi
echo "✓ 봇 임시 실행 중 (PID=$BOT_PID)"
echo

read -p "chat_id (숫자만): " CHAT_ID

# 봇 종료
kill $BOT_PID 2>/dev/null || true
wait $BOT_PID 2>/dev/null || true

if ! [[ "$CHAT_ID" =~ ^[0-9]+$ ]]; then
    echo "✗ 올바른 숫자 아님"
    exit 1
fi

# ── 9. config.json 완성 ──────────────────────────────────────
cat > config.json <<EOF
{
  "token": "$BOT_TOKEN",
  "allowed_ids": [$CHAT_ID],
  "tmux_session": "$TMUX_NAME",
  "claude_path": "$CLAUDE_PATH"
}
EOF

echo
echo "========================================"
echo "  설치 완료"
echo "========================================"
echo
echo "  ./start.sh  - 백그라운드 실행"
echo "  ./stop.sh   - 종료"
echo
