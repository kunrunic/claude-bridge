"""
Step 1 (pipe-pane redesign PoC) — tmux.start_pipe_pane / stop_pipe_pane 단위 테스트.

docs/plans/20260419-pipe-pane-redesign-poc/ 참조.

검증 범위:
  - start_pipe_pane 이 올바른 tmux 인자를 만드는지 (pipe-pane, target-pane '=name:')
  - 로그 경로가 shlex.quote 로 감싸져 공백/특수문자 안전한지
  - stop_pipe_pane 이 command 없이 호출되는지 (pipe 해제)
  - BRIDGE_PIPE_PANE=0 시 start 가 no-op
  - target=None 이면 config.TMUX 기본값 사용
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
import asyncio
import pytest


def _stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _ensure_bridge_importable(tmux_session: str = "claude_bridge_test"):
    """bot 의존성을 stub 처리해 bridge 패키지만 import 가능하게."""
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))

    class _DummyMeta(type):
        def __getattr__(cls, _): return cls

    class _Dummy(metaclass=_DummyMeta):
        def __init__(self, *a, **kw): pass
        def __call__(self, *a, **kw): return self
        def __getattr__(self, _): return self

    tg = _stub("telegram")
    tg.Update = _Dummy
    tg.InlineKeyboardButton = _Dummy
    tg.InlineKeyboardMarkup = _Dummy
    tg.BotCommand = _Dummy
    _stub("telegram.ext",
          Application=_Dummy, ApplicationBuilder=_Dummy,
          CommandHandler=_Dummy, MessageHandler=_Dummy,
          CallbackQueryHandler=_Dummy, ContextTypes=_Dummy, filters=_Dummy())
    _stub("telegram.request", HTTPXRequest=_Dummy)

    cfg = root / "config.json"
    if not cfg.exists():
        import json
        cfg.write_text(json.dumps({
            "token": "x", "allowed_ids": [1],
            "tmux_session": tmux_session, "claude_path": "/usr/bin/claude"
        }))

    # 기존 테스트들과 같이 bridge 는 한 번만 import 해서 재사용.
    # sys.modules.pop(...) / invalidate_caches() 는 이후 테스트 파일들의
    # 모듈 격리를 깨트리므로 호출하지 않는다.


_ensure_bridge_importable()


def _ok_result() -> MagicMock:
    r = MagicMock()
    r.returncode = 0
    r.stdout = ""
    r.stderr = ""
    return r


def _fail_result() -> MagicMock:
    r = MagicMock()
    r.returncode = 1
    r.stdout = ""
    r.stderr = "tmux error"
    return r


# ── start_pipe_pane ─────────────────────────────────────────────────────────

def test_start_pipe_pane_builds_correct_tmux_args():
    """pipe-pane + target-pane '=<tmux>:' + 'cat >> <quoted_path>'."""
    from bridge import tmux, config

    captured: list[list[str]] = []

    async def fake_run(cmd):
        captured.append(list(cmd))
        return _ok_result()

    async def run():
        with patch.object(tmux, "tmux_run_async", side_effect=fake_run), \
             patch.object(config, "BRIDGE_PIPE_PANE_ENABLED", True):
            ok = await tmux.start_pipe_pane("/tmp/raw.log")
        return ok

    ok = asyncio.run(run())
    assert ok is True
    assert len(captured) == 1
    cmd = captured[0]
    assert cmd[0] == "pipe-pane"
    assert cmd[1] == "-t"
    # target-pane 은 '=<tmux>:' 형식 (exact-match + pane 접미콜론)
    assert cmd[2] == f"={config.TMUX}:"
    assert cmd[3] == "cat >> /tmp/raw.log"


def test_start_pipe_pane_shlex_quotes_path_with_spaces():
    from bridge import tmux, config

    captured: list[list[str]] = []

    async def fake_run(cmd):
        captured.append(list(cmd))
        return _ok_result()

    async def run():
        with patch.object(tmux, "tmux_run_async", side_effect=fake_run), \
             patch.object(config, "BRIDGE_PIPE_PANE_ENABLED", True):
            await tmux.start_pipe_pane("/tmp/my logs/raw'1.log")

    asyncio.run(run())
    shell_cmd = captured[0][3]
    # 공백/따옴표 모두 shlex.quote 로 escape — 단순 '>> path' 가 아니어야 한다
    assert "/tmp/my logs/raw'1.log" not in shell_cmd or shell_cmd.count("'") > 2
    # 명시적 확인: shlex.quote 는 작은따옴표 내부를 '"'"' 로 감싼다
    assert "'\"'\"'" in shell_cmd


def test_start_pipe_pane_uses_custom_target_when_given():
    from bridge import tmux, config

    captured: list[list[str]] = []

    async def fake_run(cmd):
        captured.append(list(cmd))
        return _ok_result()

    async def run():
        with patch.object(tmux, "tmux_run_async", side_effect=fake_run), \
             patch.object(config, "BRIDGE_PIPE_PANE_ENABLED", True):
            await tmux.start_pipe_pane("/tmp/x.log", target="other_session")

    asyncio.run(run())
    assert captured[0][2] == "=other_session:"


def test_start_pipe_pane_disabled_is_noop():
    from bridge import tmux, config

    run_calls = []

    async def fake_run(cmd):
        run_calls.append(cmd)
        return _ok_result()

    async def run():
        with patch.object(tmux, "tmux_run_async", side_effect=fake_run), \
             patch.object(config, "BRIDGE_PIPE_PANE_ENABLED", False):
            return await tmux.start_pipe_pane("/tmp/x.log")

    ok = asyncio.run(run())
    assert ok is False
    assert run_calls == []


def test_start_pipe_pane_returns_false_on_tmux_error():
    from bridge import tmux, config

    async def fake_run(cmd):
        return _fail_result()

    async def run():
        with patch.object(tmux, "tmux_run_async", side_effect=fake_run), \
             patch.object(config, "BRIDGE_PIPE_PANE_ENABLED", True):
            return await tmux.start_pipe_pane("/tmp/x.log")

    ok = asyncio.run(run())
    assert ok is False


def test_start_pipe_pane_swallows_exception():
    """tmux_run_async 예외가 monitor loop 로 전파되면 안 됨."""
    from bridge import tmux, config

    async def fake_run(cmd):
        raise RuntimeError("boom")

    async def run():
        with patch.object(tmux, "tmux_run_async", side_effect=fake_run), \
             patch.object(config, "BRIDGE_PIPE_PANE_ENABLED", True):
            return await tmux.start_pipe_pane("/tmp/x.log")

    ok = asyncio.run(run())
    assert ok is False


# ── stop_pipe_pane ──────────────────────────────────────────────────────────

def test_stop_pipe_pane_calls_tmux_without_command():
    """pipe-pane 을 command 없이 호출 → 기존 pipe 해제."""
    from bridge import tmux, config

    captured: list[list[str]] = []

    async def fake_run(cmd):
        captured.append(list(cmd))
        return _ok_result()

    async def run():
        with patch.object(tmux, "tmux_run_async", side_effect=fake_run):
            await tmux.stop_pipe_pane()

    asyncio.run(run())
    assert len(captured) == 1
    cmd = captured[0]
    assert cmd[0] == "pipe-pane"
    assert cmd[1] == "-t"
    assert cmd[2] == f"={config.TMUX}:"
    # command 인자 없음 — 4번째 요소가 있으면 그것이 새 pipe 의 shell cmd 가 됨
    assert len(cmd) == 3


def test_stop_pipe_pane_swallows_exception():
    from bridge import tmux

    async def fake_run(cmd):
        raise RuntimeError("boom")

    async def run():
        with patch.object(tmux, "tmux_run_async", side_effect=fake_run):
            # 예외 전파되지 않아야 함
            await tmux.stop_pipe_pane()

    asyncio.run(run())  # no exception
