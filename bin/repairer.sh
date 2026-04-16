#!/bin/bash
# claude-bridge 수리 도구
# bugreporter 가 작성한 BUG_REPORT.md 를 읽어 실제 코드에 수정안을 적용한다.
# 프로젝트 루트에서 Claude 세션을 시작하므로 소스를 직접 편집할 수 있다.
#
# 사용:
#   ./bin/repairer.sh                       # 가장 최근 bugreport 사용
#   ./bin/repairer.sh bugreport/20260416_153012   # 특정 리포트 지정

set -e
cd "$(dirname "$0")/.."

# ── 리포트 디렉토리 결정 ──────────────────────────────────────
if [ -n "$1" ]; then
    REPORT_DIR="$1"
else
    REPORT_DIR=$(find bugreport -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | tail -1)
fi

if [ -z "$REPORT_DIR" ] || [ ! -d "$REPORT_DIR" ]; then
    echo "오류: bugreport 디렉토리를 찾을 수 없습니다."
    echo "먼저 ./bin/bugreporter.sh 를 실행해 버그 리포트를 작성하세요."
    exit 1
fi

if [ ! -f "$REPORT_DIR/BUG_REPORT.md" ]; then
    echo "오류: $REPORT_DIR/BUG_REPORT.md 가 없습니다."
    echo "bugreporter 가 아직 BUG_REPORT.md 를 작성하지 못한 상태입니다."
    echo "bugreporter 세션을 끝까지 진행해서 해당 파일을 만든 뒤 재실행하세요."
    exit 1
fi

TS=$(date +%Y%m%d_%H%M%S)
REPORT_ABS=$(cd "$REPORT_DIR" && pwd)

echo "========================================"
echo "  claude-bridge repairer"
echo "  리포트: $REPORT_DIR"
echo "  작업 시각: $TS"
echo "========================================"
echo

# ── 안전 체크: uncommitted 변경 경고 ─────────────────────────
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    echo "주의: 현재 작업 트리에 미커밋 변경이 있습니다."
    echo "수리 전에 커밋하거나 stash 해두는 것을 권장합니다."
    echo
    read -p "계속 진행할까요? [y/N] " yn
    yn=${yn:-N}
    if ! [[ "$yn" =~ ^[Yy]$ ]]; then
        echo "취소됨."
        exit 1
    fi
fi

# ── CLAUDE.md 생성 (프로젝트 루트에 임시 가이드) ─────────────
# 이미 CLAUDE.md 가 있으면 백업 후 복구
PROJECT_CLAUDE_MD="CLAUDE.md"
BACKUP=""
if [ -f "$PROJECT_CLAUDE_MD" ]; then
    BACKUP="${PROJECT_CLAUDE_MD}.repairer_backup_${TS}"
    cp "$PROJECT_CLAUDE_MD" "$BACKUP"
fi

cat > "$PROJECT_CLAUDE_MD" <<EOF
# claude-bridge 수리 세션

- 시각: $(date '+%Y-%m-%d %H:%M:%S')
- 참조 리포트: ${REPORT_ABS}/BUG_REPORT.md

## 너의 역할

이 세션은 **코드 수리** 목적이다.
${REPORT_ABS}/BUG_REPORT.md 의 **제안 수정안** 섹션을 읽고, 실제 프로젝트 파일에 반영한다.

## 작업 순서

1. **리포트 정독** — ${REPORT_ABS}/BUG_REPORT.md 를 먼저 Read. 현상/원인/제안된 diff 를 모두 파악한다.
2. **스코프 재확인** — 제안된 수정안이 현재 브랜치와 호환되는지 확인. 파일 경로/함수명이 리포트 작성 이후 변경됐을 수 있다.
3. **수정 적용** — 제안 diff 를 Edit 도구로 실제 파일에 적용.
4. **테스트 추가/수정** — 리포트가 제안한 테스트 추가/변경도 함께 반영.
5. **테스트 실행** — \`source venv/bin/activate && python -m pytest tests/ -q\` 을 돌려 회귀 확인.
6. **커밋 메시지 초안 제시** — 커밋은 사용자 지시 없이는 수행하지 않는다. 대신 제안만 출력.
   - Co-Authored-By 트레일러는 붙이지 않는다 (프로젝트 규칙).

## 원칙

- 리포트에 명시되지 않은 리팩토링/정리 작업은 하지 않는다 (scope creep 금지).
- 테스트가 실패하면 원인을 설명하고 사용자에게 판단을 요청한다 (억지로 통과시키지 말 것).
- 수정 대상 파일이 여러 개여도 한 번에 한 파일씩 Read → 이해 → Edit.

## 첫 행동

${REPORT_ABS}/BUG_REPORT.md 를 Read 도구로 읽고, 수리 범위 요약 (변경할 파일 목록과 한 줄 요지) 을
사용자에게 먼저 보고한 뒤 "적용할까요?" 라고 묻는다. 사용자 승인 후에 Edit 시작.
EOF

echo "CLAUDE.md 임시 가이드 생성됨."
if [ -n "$BACKUP" ]; then
    echo "기존 CLAUDE.md 는 $BACKUP 으로 백업됨."
fi
echo

# ── Claude Code 실행 ────────────────────────────────────────
CLAUDE_PATH=$(python3 -c "import json; print(json.load(open('config.json'))['claude_path'])" 2>/dev/null || echo "claude")

echo "Claude Code 를 대화형으로 시작합니다."
echo "(프로젝트 루트에서 실행 - 실제 소스 편집 가능)"
echo "종료 후 CLAUDE.md 는 원복됩니다."
echo

# trap 으로 종료 시 CLAUDE.md 원복
restore_claude_md() {
    if [ -n "$BACKUP" ] && [ -f "$BACKUP" ]; then
        mv "$BACKUP" "$PROJECT_CLAUDE_MD"
        echo
        echo "CLAUDE.md 원복됨."
    else
        rm -f "$PROJECT_CLAUDE_MD"
        echo
        echo "임시 CLAUDE.md 제거됨."
    fi
}
trap restore_claude_md EXIT INT TERM

"$CLAUDE_PATH" "CLAUDE.md 를 먼저 읽고, ${REPORT_ABS}/BUG_REPORT.md 의 제안 수정안을 요약해 '적용할까요?' 하고 물어봐줘."
