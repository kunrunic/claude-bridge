"""tmux 원시 조작 — capture-pane, send-keys 등.

이 모듈은 외부 프로세스와 직접 통신하는 유일한 계층이다.
parser 는 이 모듈의 stdout 결과를 받아 해석한다.
"""
from __future__ import annotations

import asyncio
import shlex
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


# -- pipe-pane raw 로그 수집 (Step 1 PoC) -------------------------------------
# docs/plans/20260419-pipe-pane-redesign-poc/fixes.md 참조.
# 기존 capture-pane 기반 monitor loop 와 병행. 실패가 loop 를 멈추지 않는다.

async def start_pipe_pane(log_path: str, target: str | None = None) -> bool:
    """tmux pipe-pane 으로 raw ANSI 스트림을 파일에 append.

    옵션 없이 호출하면 기존 pipe 가 있어도 새 command 로 교체된다 (`man tmux`).
    Returns True on success, False if disabled or tmux 오류 (loop 는 계속).
    """
    if not config.BRIDGE_PIPE_PANE_ENABLED:
        return False
    quoted = shlex.quote(log_path)
    shell_cmd = f"cat >> {quoted}"
    try:
        r = await tmux_run_async(
            ["pipe-pane", "-t", _tp(target), shell_cmd]
        )
        if r.returncode == 0:
            dump.event("tmux", "pipe_pane_start", target=_tp(target), log_path=log_path)
            return True
        _log("PIPE-PANE-START-FAIL",
             f"rc={r.returncode} err={(r.stderr or '')[:200]}")
        return False
    except Exception as e:
        _log("PIPE-PANE-START-ERROR", str(e)[:200])
        return False


async def stop_pipe_pane(target: str | None = None) -> None:
    """기존 pipe 해제. command 인자 없이 pipe-pane 호출."""
    try:
        await tmux_run_async(["pipe-pane", "-t", _tp(target)])
        dump.event("tmux", "pipe_pane_stop", target=_tp(target))
    except Exception as e:
        _log("PIPE-PANE-STOP-ERROR", str(e)[:200])
