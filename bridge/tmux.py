"""tmux 원시 조작 — capture-pane, send-keys 등.

이 모듈은 외부 프로세스와 직접 통신하는 유일한 계층이다.
parser 는 이 모듈의 stdout 결과를 받아 해석한다.
"""
from __future__ import annotations

import asyncio
import subprocess
import time

from . import config
from .config import _log


def tmux_run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["/opt/homebrew/bin/tmux"] + cmd,
            capture_output=True, text=True, timeout=5.0,
        )
    except subprocess.TimeoutExpired:
        _log("TMUX-TIMEOUT", f"cmd={cmd[:3]}")
        r = subprocess.CompletedProcess(cmd, returncode=1)
        r.stdout = ""
        r.stderr = "timeout"
        return r


async def tmux_run_async(cmd: list[str]) -> subprocess.CompletedProcess:
    """asyncio 이벤트 루프를 블로킹하지 않는 tmux_run."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, tmux_run, cmd)


def pane_output() -> str:
    """현재 tmux 패널 내용 반환 (마지막 TMUX_SCROLL_LINES줄)."""
    return tmux_run(
        ["capture-pane", "-t", config.TMUX, "-p", "-S", f"-{config.TMUX_SCROLL_LINES}"]
    ).stdout


async def pane_output_async() -> str:
    """pane_output 의 비동기 버전."""
    r = await tmux_run_async(
        ["capture-pane", "-t", config.TMUX, "-p", "-S", f"-{config.TMUX_SCROLL_LINES}"]
    )
    return r.stdout


def send_input(text: str):
    """Claude 에 텍스트 입력 후 Enter (literal 모드로 안전하게)."""
    tmux_run(["send-keys", "-t", config.TMUX, "-l", text])
    time.sleep(0.1)
    tmux_run(["send-keys", "-t", config.TMUX, "Enter"])


def send_key(key: str):
    """Enter / Down / Escape 등 특수 키 전송."""
    tmux_run(["send-keys", "-t", config.TMUX, key])
