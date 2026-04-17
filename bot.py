#!/usr/bin/env python3
"""claude-bridge 엔트리 포인트.

구조:
    bridge/config   — 상수/설정/로거/권한 게이트
    bridge/tmux     — tmux 원시 조작
    bridge/parser   — pure 파싱 함수
    bridge/session  — Claude 세션 파일 + 락
    bridge/sender   — outbound (봇 → 유저)
    bridge/core     — Bridge + monitor 루프
    bridge/receiver — inbound (cmd_*, on_message, on_callback)

이 파일은 Application 을 만들고 핸들러를 바인딩한 뒤 폴링만 돈다.
기존 테스트는 `import bot` 후 `bot.<symbol>` 로 접근하므로,
호환성을 위해 필요한 심볼을 모두 re-export 한다.
"""
from __future__ import annotations

import asyncio
import sys
import time

from telegram import BotCommand
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    MessageHandler, filters,
)
from telegram.request import HTTPXRequest

# -- 서브모듈 re-export (테스트/외부 호환) -----------------------------------

# 하위 모듈 자체를 bot 네임스페이스에도 노출 — 테스트가 `bot.tmux.send_key` 등
# 정의 위치 기준으로 patch 할 수 있도록.
from bridge import config, core, dump, parser, receiver, sender, session, tmux

from bridge.config import (
    ALLOWED_IDS,
    APPROVAL_SCAN_LINES,
    BUSY_CHECK_TAIL,
    BUSY_STREAM_SEC,
    BUSY_STUCK_SEC,
    BUSY_TIMEOUT_SEC,
    CLAUDE,
    COMPACT_SCAN_LINES,
    MAX_MSG_CHARS,
    PROJECTS,
    SETTLE_TICKS,
    STOP_WAIT_SEC,
    TMUX,
    TMUX_SCROLL_LINES,
    TOKEN,
    _log,
    deny,
    is_allowed,
)
from bridge.tmux import (
    pane_output,
    pane_output_async,
    send_input,
    send_key,
    tmux_run,
    tmux_run_async,
)
from bridge.parser import (
    ANSI_RE,
    APPROVAL_RE,
    BUSY_RE,
    COMPACT_ERROR_RE,
    COMPACT_RE,
    CONTEXT_LIMIT_RE,
    LIMIT_RE,
    RESUME_PICKER_RE,
    TRUST_RE,
    _approval_box,
    _find_picker_cursor,
    _format_busy_status,
    _is_block_active,
    _parse_model_options,
    _response_region,
    busy_status,
    extract_last_response,
    extract_response_blocks,
    has_compaction,
    has_compaction_error,
    has_context_limit,
    is_approval,
    is_busy,
    is_resume_picker,
    is_trust_prompt,
    strip_ansi,
    summarize_approval,
)
from bridge.session import (
    _acquire_lock,
    _is_locked,
    _lock_path,
    _my_locks,
    _parse_lock,
    _release_lock,
    find_sessions,
    get_session_cwd,
)
from bridge.sender import (
    _chunk_text,
    _render_model_picker,
    _send_approval,
    _send_model_switch_prompt,
    _send_output,
)
from bridge.core import Bridge, bridge
from bridge.receiver import (
    IMAGE_DIR,
    _build_start_kb,
    cmd_end,
    cmd_esc,
    cmd_model,
    cmd_start,
    cmd_unlock,
    cmd_whoami,
    on_callback,
    on_message,
)


# -- 진입점 -------------------------------------------------------------------

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",  "세션 목록 / 새 세션 시작"),
        BotCommand("end",    "현재 세션 종료"),
        BotCommand("esc",    "ESC 키 전송 (취소/중단)"),
        BotCommand("model",  "모델 변경 피커 열기"),
        BotCommand("unlock", "세션 락 강제 해제"),
        BotCommand("whoami", "내 chat_id 확인 (관리자 등록용)"),
    ])

    check = tmux.tmux_run(["has-session", "-t", config.TMUX])
    if check.returncode == 0 and ALLOWED_IDS:
        chat_id = next(iter(ALLOWED_IDS))
        await app.bot.send_message(chat_id, "기존 세션에 재연결되었습니다.")
        bridge.task = asyncio.create_task(bridge.monitor(app, chat_id))


def _build_app() -> Application:
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .request(HTTPXRequest(connect_timeout=10, read_timeout=15))  # P1-3
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("end",    cmd_end))
    app.add_handler(CommandHandler("esc",    cmd_esc))
    app.add_handler(CommandHandler("model",  cmd_model))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.PHOTO,
        on_message,
    ))
    return app


def main():
    # dump 초기화 (CB_DUMP env 가 있을 때만 실제 기록, 아니면 no-op)
    instance = sys.argv[1] if len(sys.argv) > 1 else "default"
    dump.init(instance)

    # P1-2: Telegram 폴링 재연결 루프 (exponential backoff)
    backoff = 1
    retries = 0
    max_retries = 10

    while True:
        try:
            app = _build_app()
            _log("BOOT", f"claude-bridge started (Ctrl+C to quit, attempt={retries + 1})")
            app.run_polling(drop_pending_updates=True)
            break  # 정상 종료 (KeyboardInterrupt 등)
        except KeyboardInterrupt:
            _log("SHUTDOWN", "Ctrl+C — 종료")
            break
        except Exception as e:
            retries += 1
            _log("POLLING-ERROR", f"재연결 시도 {retries}/{max_retries}: {e}")
            if retries >= max_retries:
                _log("POLLING-FATAL", "최대 재시도 초과 — 종료")
                sys.exit(1)
            # 이전 bridge monitor 정리
            if bridge.task:
                bridge.task.cancel()
                bridge.task = None
            bridge.running = False
            wait = min(backoff, 60)
            _log("POLLING-RETRY", f"{wait}초 후 재시도…")
            time.sleep(wait)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
