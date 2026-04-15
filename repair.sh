#!/bin/bash
# claude-bridge 진단/리페어 도구
# tmux 상태 + 로그 + 소스 스냅샷을 수집한 뒤
# 대화형 Claude Code 세션을 띄워 사용자가 직접 대화하게 합니다.
#
# 사용: ./repair.sh

set -e
cd "$(dirname "$0")"

TS=$(date +%Y%m%d_%H%M%S)
REPAIR_DIR="repair/$TS"
mkdir -p "$REPAIR_DIR"

TMUX_BIN=$(command -v tmux || echo "/opt/homebrew/bin/tmux")

echo "========================================"
echo "  claude-bridge repair mode"
echo "  ID: $TS"
echo "========================================"
echo

# ── 1. tmux 캡처 ─────────────────────────────────────────────
echo "[1/5] tmux 상태 수집..."
{
    echo "# tmux sessions"
    $TMUX_BIN list-sessions 2>&1 || echo "(no server)"
    echo
    echo "# claude_bridge pane (last 500 lines)"
    $TMUX_BIN capture-pane -t claude_bridge -p -S -500 2>&1 || echo "(no session)"
    echo
    echo "# pane dead flag"
    $TMUX_BIN list-panes -t claude_bridge -F "#{pane_dead} #{pane_pid}" 2>&1 || echo "(no session)"
} > "$REPAIR_DIR/tmux_capture.txt"

# ── 2. 최근 봇 로그 ──────────────────────────────────────────
echo "[2/5] 봇 로그 수집..."
mkdir -p "$REPAIR_DIR/logs"
for f in logs/*.log; do
    [ -f "$f" ] || continue
    # 최근 3개 파일, 각 마지막 500줄
    cp "$f" "$REPAIR_DIR/logs/" 2>/dev/null || true
done
ls -lt "$REPAIR_DIR/logs/" 2>/dev/null | head -4 | tail -3 > "$REPAIR_DIR/logs/_index.txt" 2>&1 || true

# ── 3. config.json (토큰 마스킹) ─────────────────────────────
echo "[3/5] config 수집 (민감정보 마스킹)..."
if [ -f config.json ]; then
    python3 -c "
import json
with open('config.json') as f: cfg = json.load(f)
if 'token' in cfg and cfg['token']:
    cfg['token'] = cfg['token'][:10] + '****MASKED****'
print(json.dumps(cfg, indent=2, ensure_ascii=False))
" > "$REPAIR_DIR/config_masked.json"
fi

# ── 4. 소스 스냅샷 ────────────────────────────────────────────
echo "[4/5] 소스 스냅샷..."
cp bot.py "$REPAIR_DIR/bot.py" 2>/dev/null || true
git -C . diff --stat 2>/dev/null > "$REPAIR_DIR/git_status.txt" || true
git -C . log --oneline -10 2>/dev/null >> "$REPAIR_DIR/git_status.txt" || true

# ── 5. REPAIR_CONTEXT.md 생성 ────────────────────────────────
echo "[5/5] 컨텍스트 문서 생성..."
cat > "$REPAIR_DIR/CLAUDE.md" <<EOF
# claude-bridge 리페어 세션

- 생성 시각: $(date '+%Y-%m-%d %H:%M:%S')

## 너의 역할

claude-bridge 봇의 디버깅을 담당하는 엔지니어이다.
아래 수집된 증거를 분석해서 **현재 현상 → 원인 가설 → 재현 방법 → 수정안** 구조로 버그 리포트를 작성한다.

**첫 행동**: 사용자에게 "어떤 증상인가요?" 물어본다. 사용자 답변을 받은 뒤 증거 분석을 시작한다.

## 수집된 증거

| 파일 | 내용 |
|------|------|
| \`tmux_capture.txt\` | claude_bridge tmux 패널 최근 500줄 + 세션 상태 |
| \`logs/\` | 봇 포그라운드 구조화된 로그 (USER→BOT / BOT→AI / AI-BUSY 등) |
| \`config_masked.json\` | 설정 (토큰 마스킹됨) |
| \`bot.py\` | 현재 소스 코드 스냅샷 |
| \`git_status.txt\` | 변경 상태 + 최근 커밋 |

## 작업 순서

1. **증상 청취** — 사용자에게 무슨 일이 있었는지 자연스럽게 묻는다.
2. **증거 파일 읽기** — Read 도구로 위 표의 모든 파일을 읽는다.
3. **타임라인 복원** — tmux 캡처의 마지막 화면 상태와 로그 타임스탬프를 교차 대조한다.
4. **추가 정보 요청** — 필요하면 사용자에게 명시적으로 요청한다 (예: "텔레그램 스크린샷 있어요?", "언제부터 이랬어요?", "재현되나요?").
5. **버그 리포트 작성** — 최종적으로 \`BUG_REPORT.md\` 를 이 디렉토리에 작성한다.

## BUG_REPORT.md 양식

\`\`\`markdown
# 버그 리포트 — ${TS}

## 현상
(사용자가 겪은 일을 1-2줄로)

## 타임라인 재구성
| 시각 | 소스 | 이벤트 |
|------|------|--------|
| HH:MM:SS | log | USER→BOT "..." |
| HH:MM:SS | tmux | (화면에 표시된 내용) |
...

## 원인 가설
1. **가설 A** — 근거: (로그 X줄, tmux Y줄)
2. **가설 B** — 근거: ...

## 재현 방법
1. ...

## 수정안
### bot.py 변경
\`\`\`diff
- 기존 코드
+ 새 코드
\`\`\`

### 리스크 / 사이드이펙트
...

## 추가 수집이 필요한 정보
- [ ] ...
\`\`\`

---

**시작**: 먼저 사용자에게 "어떤 증상이 있었나요? 시간대와 함께 말해주세요." 라고 물어본다.
EOF

echo
echo "========================================"
echo "  증거 수집 완료"
echo "========================================"
echo "  경로: $REPAIR_DIR/"
echo
ls -la "$REPAIR_DIR/"
echo

# ── 6. Claude Code 대화형 세션 실행 ──────────────────────────
CLAUDE_PATH=$(python3 -c "import json; print(json.load(open('config.json'))['claude_path'])" 2>/dev/null || echo "claude")

echo "Claude Code 를 대화형으로 시작합니다."
echo "(이 디렉토리의 CLAUDE.md 를 자동 로드하며 Claude가 먼저 증상을 물어봅니다)"
echo

cd "$REPAIR_DIR"
# 초기 프롬프트: 대화형 모드로 진입하면서 첫 사용자 메시지로 지시
exec "$CLAUDE_PATH" "CLAUDE.md 를 먼저 읽고, 나에게 '어떤 증상이 있었나요? 시간대와 함께 알려주세요' 라고 물어봐줘."
