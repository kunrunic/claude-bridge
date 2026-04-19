"""상수, 설정 파일 로드, 로거, 권한 게이트."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from telegram import Update

# -- 상수 -----------------------------------------------------------------------

TMUX_SCROLL_LINES   = 500   # pane 캡처 줄 수 — position-based 큐 가드용 (스크롤백 여유)
APPROVAL_SCAN_LINES = 30    # 승인 박스 스캔 범위
SETTLE_TICKS        = 2     # 화면 안정화 틱 수
BUSY_STREAM_SEC     = 10    # bypass 모드 등 장시간 busy 중 ⏺ 블록 주기적 포워딩 간격
STOP_WAIT_SEC       = 5     # stop() /exit 후 대기 초
MAX_MSG_CHARS       = 3500  # Telegram 메시지 최대 길이
BUSY_CHECK_TAIL     = 20    # busy 감지 꼬리 줄 수
BUSY_TIMEOUT_SEC    = 600   # busy 최대 지속 시간 — 초과 시 watchdog 발동 (P0)
BUSY_STUCK_SEC      = 300   # pane 내용 무변화 지속 시 stuck 판정 (P2)
COMPACT_SCAN_LINES  = 8     # 압축 이벤트 감지 스캔 범위 (현재 활성 영역만)

# -- pipe-pane raw 로그 (Step 1 of pipe-pane redesign PoC) ---------------------
# docs/plans/20260419-pipe-pane-redesign-poc/ 참조.
# 관찰 데이터만 수집, 기존 monitor loop 동작은 무변경.
BRIDGE_PIPE_PANE_ENABLED: bool = os.getenv("BRIDGE_PIPE_PANE", "1") != "0"
BRIDGE_PIPE_PANE_DIR: Path     = Path.home() / ".claude-bridge" / "panes"
BRIDGE_PIPE_PANE_MAX_BYTES: int = 20 * 1024 * 1024   # 20MB → rotate
BRIDGE_PIPE_PANE_ROTATE_CHECK_SEC: float = 30.0      # 크기 체크 최소 간격
# §13.4 Offset 무결성 safeguard — pre-flight disk check.
# BRIDGE_PIPE_PANE_DIR 의 가용 공간이 이 값(MB) 미만이면 pipe attach 거부.
BRIDGE_PIPE_PANE_MIN_FREE_MB: int = int(os.getenv("BRIDGE_PIPE_PANE_MIN_FREE_MB", "500"))

# -- Shadow run 분석기 (Step 4-α) ---------------------------------------------
# BRIDGE_SHADOW_ANALYZER=1 로 활성화. 기본 OFF — 의도적으로 opt-in 해야 shadow
# run 이 시작되도록. 활성화 시 monitor 루프가 pipe-pane raw 를 tail 해
# analyzer-events.jsonl 에 기록. Telegram dispatch 는 하지 않음 (관찰 전용).
BRIDGE_SHADOW_ANALYZER: bool = os.getenv("BRIDGE_SHADOW_ANALYZER", "0") == "1"

# -- 설정 -----------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent.parent / "config.json"


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        sys.exit(f"[FATAL] config.json 없음: {_CONFIG_PATH}")
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"[FATAL] config.json 파싱 오류: {e}")
    for key in ("token", "claude_path", "allowed_ids"):
        if key not in cfg:
            sys.exit(f"[FATAL] config.json 필수 항목 누락: {key}")
    return cfg


_cfg = _load_config()

TOKEN       = _cfg["token"]
TMUX        = _cfg.get("tmux_session", "claude_bridge")
CLAUDE      = _cfg["claude_path"]
PROJECTS    = Path.home() / ".claude" / "projects"
ALLOWED_IDS: set[int] = set(_cfg.get("allowed_ids", []))


def _log(tag: str, msg: str = ""):
    """구조화된 포그라운드 로그."""
    ts = time.strftime("%H:%M:%S")
    if msg:
        print(f"[{ts}] [{tag}] {msg}")
    else:
        print(f"[{ts}] [{tag}]")


def is_allowed(update) -> bool:
    return update.effective_chat.id in ALLOWED_IDS


async def deny(update: Update):
    await update.effective_message.reply_text(
        f"접근 거부. 이 봇은 등록된 사용자만 사용할 수 있습니다.\n"
        f"본인 확인용 ID: {update.effective_chat.id}"
    )
