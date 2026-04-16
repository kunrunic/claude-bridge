#!/bin/bash
# claude-bridge 버그 리포터
# tmux 상태 + 로그 + 소스 스냅샷 (+ 최근 dump) 을 수집한 뒤
# 대화형 Claude Code 세션을 띄워 분석 → 수정안 제안을 BUG_REPORT.md 로 작성한다.
# 실제 코드 수정은 수행하지 않는다. 수정은 ./bin/repairer.sh 가 담당.
#
# 사용: ./bin/bugreporter.sh

set -e
cd "$(dirname "$0")/.."

TS=$(date +%Y%m%d_%H%M%S)
REPORT_DIR="bugreport/$TS"
mkdir -p "$REPORT_DIR"

TMUX_BIN=$(command -v tmux || echo "/opt/homebrew/bin/tmux")

# 이 인스턴스의 tmux 세션명 읽기 (없으면 기본값)
TMUX_SESSION=$(python3 -c "import json; print(json.load(open('config.json')).get('tmux_session','claude_bridge'))" 2>/dev/null || echo "claude_bridge")

echo "========================================"
echo "  claude-bridge bugreporter"
echo "  ID: $TS"
echo "  tmux session: $TMUX_SESSION"
echo "========================================"
echo

# ── 1. tmux 캡처 ─────────────────────────────────────────────
echo "[1/6] tmux 상태 수집..."
{
    echo "# tmux sessions"
    $TMUX_BIN list-sessions 2>&1 || echo "(no server)"
    echo
    echo "# $TMUX_SESSION pane (last 500 lines)"
    $TMUX_BIN capture-pane -t "$TMUX_SESSION" -p -S -500 2>&1 || echo "(no session)"
    echo
    echo "# pane dead flag"
    $TMUX_BIN list-panes -t "$TMUX_SESSION" -F "#{pane_dead} #{pane_pid}" 2>&1 || echo "(no session)"
} > "$REPORT_DIR/tmux_capture.txt"

# ── 2. 최근 봇 로그 ──────────────────────────────────────────
echo "[2/6] 봇 로그 수집..."
mkdir -p "$REPORT_DIR/logs"
for f in logs/*.log; do
    [ -f "$f" ] || continue
    cp "$f" "$REPORT_DIR/logs/" 2>/dev/null || true
done
ls -lt "$REPORT_DIR/logs/" 2>/dev/null | head -4 | tail -3 > "$REPORT_DIR/logs/_index.txt" 2>&1 || true

# ── 3. config.json (토큰 마스킹) ─────────────────────────────
echo "[3/6] config 수집 (민감정보 마스킹)..."
if [ -f config.json ]; then
    python3 -c "
import json
with open('config.json') as f: cfg = json.load(f)
if 'token' in cfg and cfg['token']:
    cfg['token'] = cfg['token'][:10] + '****MASKED****'
print(json.dumps(cfg, indent=2, ensure_ascii=False))
" > "$REPORT_DIR/config_masked.json"
fi

# ── 4. 소스 스냅샷 ────────────────────────────────────────────
echo "[4/6] 소스 스냅샷..."
cp bot.py "$REPORT_DIR/bot.py" 2>/dev/null || true
if [ -d bridge ]; then
    mkdir -p "$REPORT_DIR/bridge"
    cp bridge/*.py "$REPORT_DIR/bridge/" 2>/dev/null || true
fi
git -C . diff --stat 2>/dev/null > "$REPORT_DIR/git_status.txt" || true
git -C . log --oneline -10 2>/dev/null >> "$REPORT_DIR/git_status.txt" || true

# ── 5. dump 자동 포함 (최근 1시간 내에 기록된 경우) ──────────
echo "[5/6] dump 증거 확인..."
DUMP_NOTE="(dump 없음 — 스트리밍 이슈면 ./bin/stop.sh && ./bin/start.sh --dump 로 재기동 후 재현 필요)"
if [ -d dump ]; then
    RECENT_DUMP_DIR=$(find dump -mindepth 2 -maxdepth 2 -type d -mmin -60 2>/dev/null | sort | tail -1)
    if [ -n "$RECENT_DUMP_DIR" ]; then
        mkdir -p "$REPORT_DIR/dump"
        cp -R "$RECENT_DUMP_DIR"/. "$REPORT_DIR/dump/" 2>/dev/null || true
        DUMP_NOTE="최근 dump 포함: $RECENT_DUMP_DIR"
    fi
fi
echo "  $DUMP_NOTE"

# ── 6. CLAUDE.md 생성 ───────────────────────────────────────
echo "[6/6] 컨텍스트 문서 생성..."
cat > "$REPORT_DIR/CLAUDE.md" <<EOF
# claude-bridge 버그 리포트 세션

- 생성 시각: $(date '+%Y-%m-%d %H:%M:%S')
- ID: ${TS}

## 너의 역할

claude-bridge 봇의 **증거 분석 + 원인 가설 + 수정안 제안** 을 담당한다.
**실제 코드는 수정하지 않는다.** 수정은 별도 도구 (\`./bin/repairer.sh\`) 가
이 디렉토리의 \`BUG_REPORT.md\` 를 읽어서 수행한다.

첫 행동: 사용자에게 "어떤 증상인가요? 시간대와 함께 알려주세요." 라고 물어본다.
답변을 받은 뒤 증거 분석을 시작한다.

## 수집된 증거

| 파일 | 내용 |
|------|------|
| \`tmux_capture.txt\` | $TMUX_SESSION tmux 패널 최근 500줄 + 세션 상태 |
| \`logs/\` | 봇 포그라운드 구조화된 로그 (USER→BOT / BOT→AI / AI-BUSY 등) |
| \`config_masked.json\` | 설정 (토큰 마스킹됨) |
| \`bot.py\`, \`bridge/*.py\` | 현재 소스 코드 스냅샷 |
| \`git_status.txt\` | 변경 상태 + 최근 커밋 |
| \`dump/\` (있을 때만) | 0.5s pane 스냅샷 + tmux/sender/receiver/core 이벤트 로그 |

### dump 활용법 (있을 때)

- \`dump/pane_tick.jsonl\` — 시간순 pane 변화 (hash-dedup 됨; idle 구간 압축)
- \`dump/events.jsonl\` — 각 컴포넌트가 뭘 했는지의 ground truth.
  - \`source=tmux, kind=send_input/send_key\` — Claude 에 실제로 전송된 입력
  - \`source=sender, kind=send_output\` — 사용자에게 실제로 나간 본문 + preview
  - \`source=core, kind=flush_peek\` — "완료 블록 N개 중 새 블록 M개" 로 queue 가 뭘 골랐는지
  - \`source=core, kind=flush_block\` — 실제로 _send_output 에 넘긴 블록 preview
  - \`source=receiver, kind=on_message/callback\` — 사용자 입력 시각

**스트리밍 누락 조사**: pane_tick 에서 \`⏺\` 로 시작하는 블록을 모두 뽑아 timeline 을 만들고,
events.jsonl 의 \`flush_block\` 과 교차 대조해 "pane 에 있었는데 send_output 에 안 실린 블록"을 찾는다.

## 작업 순서

1. **증상 청취** — 사용자에게 무슨 일이 있었는지 물어본다.
2. **증거 파일 읽기** — Read 도구로 위 표의 모든 파일을 읽는다 (dump/ 존재 시 필수).
3. **타임라인 복원** — tmux 캡처 / logs / (있으면) dump 를 교차 대조.
4. **추가 정보 요청** — 필요하면 사용자에게 텔레그램 스크린샷, 재현 여부 등을 요청.
5. **BUG_REPORT.md 작성** — 최종 산출물을 이 디렉토리에 작성. 절대 프로젝트의 실제 파일을 수정하지 말 것.

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
| HH:MM:SS | dump | flush_peek tag=... new_count=... |
...

## 원인 가설
1. **가설 A** — 근거: (로그 X줄, tmux Y줄, dump Z줄)
2. **가설 B** — 근거: ...

## 재현 방법
1. ...

## 제안 수정안
(실제 파일은 건드리지 않는다. 아래 diff 는 제안일 뿐이며 repairer.sh 가 적용한다.)

### bridge/xxx.py 변경
\`\`\`diff
- 기존 코드
+ 새 코드
\`\`\`

### 리스크 / 사이드이펙트
...

### 테스트 추가/변경 제안
- [ ] tests/xxx.py 에 ... 추가

## 추가 수집이 필요한 정보
- [ ] ...
\`\`\`

---

시작: 먼저 사용자에게 "어떤 증상이 있었나요? 시간대와 함께 말해주세요." 라고 물어본다.
EOF

echo
echo "========================================"
echo "  증거 수집 완료"
echo "========================================"
echo "  경로: $REPORT_DIR/"
echo "  $DUMP_NOTE"
echo
ls -la "$REPORT_DIR/"
echo

# ── 7. Claude Code 대화형 세션 실행 ──────────────────────────
CLAUDE_PATH=$(python3 -c "import json; print(json.load(open('config.json'))['claude_path'])" 2>/dev/null || echo "claude")

echo "Claude Code 를 대화형으로 시작합니다."
echo "(이 디렉토리의 CLAUDE.md 를 자동 로드하며 증상을 먼저 물어봅니다)"
echo "(실제 파일 수정은 없음 - BUG_REPORT.md 작성 후 ./bin/repairer.sh 로 수리 착수)"
echo

cd "$REPORT_DIR"
exec "$CLAUDE_PATH" "CLAUDE.md 를 먼저 읽고, 나에게 '어떤 증상이 있었나요? 시간대와 함께 알려주세요' 라고 물어봐줘."
