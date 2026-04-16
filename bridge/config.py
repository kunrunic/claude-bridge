"""상수, 설정 파일 로드, 로거, 권한 게이트."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from telegram import Update

# -- 상수 -----------------------------------------------------------------------

TMUX_SCROLL_LINES   = 200   # pane 캡처 줄 수
APPROVAL_SCAN_LINES = 30    # 승인 박스 스캔 범위
SETTLE_TICKS        = 2     # 화면 안정화 틱 수
BUSY_STREAM_SEC     = 10    # bypass 모드 등 장시간 busy 중 ⏺ 블록 주기적 포워딩 간격
STOP_WAIT_SEC       = 5     # stop() /exit 후 대기 초
MAX_MSG_CHARS       = 3500  # Telegram 메시지 최대 길이
BUSY_CHECK_TAIL     = 20    # busy 감지 꼬리 줄 수
BUSY_TIMEOUT_SEC    = 600   # busy 최대 지속 시간 — 초과 시 watchdog 발동 (P0)
BUSY_STUCK_SEC      = 300   # pane 내용 무변화 지속 시 stuck 판정 (P2)
COMPACT_SCAN_LINES  = 8     # 압축 이벤트 감지 스캔 범위 (현재 활성 영역만)

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
