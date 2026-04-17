"""tmux 원시 조작 — capture-pane, send-keys 등.

이 모듈은 외부 프로세스와 직접 통신하는 유일한 계층이다.
parser 는 이 모듈의 stdout 결과를 받아 해석한다.
"""
from __future__ import annotations

import asyncio
import subprocess
import time

from . import config, dump
from .config import _log


def _ts(name: str | None = None) -> str:
    """target-session 을 exact-match 로 강제 ('=<name>').

    has-session / kill-session / list-panes / set-option 처럼 tmux 가
    target-session 을 받는 명령에 쓴다. prefix-match 로 sibling (예:
    claude_bridge → claude_bridge2) 을 오인 조작하는 사고를 막는다.
    """
    return f"={name if name is not None else config.TMUX}"


def _tp(name: str | None = None) -> str:
    """target-pane 을 exact-match 로 강제 ('=<name>:').

    send-keys / capture-pane 처럼 tmux 가 target-pane 을 받는 명령에 쓴다.
    '=<name>' (target-session 전용) 을 그대로 넘기면 "can't find pane"
    으로 실패하므로 콜론을 붙여 "세션 <name>, 기본 window, 기본 pane"
    형태로 지정한다. '=' 덕분에 exact-match 는 그대로 유지.
    """
    return f"={name if name is not None else config.TMUX}:"


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
        ["capture-pane", "-t", _tp(), "-p", "-S", f"-{config.TMUX_SCROLL_LINES}"]
    ).stdout


async def pane_output_async() -> str:
    """pane_output 의 비동기 버전."""
    r = await tmux_run_async(
        ["capture-pane", "-t", _tp(), "-p", "-S", f"-{config.TMUX_SCROLL_LINES}"]
    )
    return r.stdout


def send_input(text: str):
    """Claude 에 텍스트 입력 후 Enter (literal 모드로 안전하게)."""
    dump.event("tmux", "send_input", text=text[:500], length=len(text))
    tmux_run(["send-keys", "-t", _tp(), "-l", text])
    time.sleep(0.1)
    tmux_run(["send-keys", "-t", _tp(), "Enter"])


def send_key(key: str):
    """Enter / Down / Escape 등 특수 키 전송."""
    dump.event("tmux", "send_key", key=key)
    tmux_run(["send-keys", "-t", _tp(), key])
