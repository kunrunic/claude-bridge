"""
tmux 세션 갑작스러운 종료 케이스 테스트.

monitor 루프의 두 가지 죽음 감지 경로:
  A) has-session returncode != 0  → tmux 세션 자체가 사라짐
  B) is_alive() == False          → tmux 세션은 있지만 pane이 죽음 (remain-on-exit)

그리고 죽음 이후 /start 시 락 상태 검증.
"""
from __future__ import annotations

import sys
import types
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock, call
import pytest


# ── stub / loader ─────────────────────────────────────────────────────────────

def _stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _load_bot(tmux_session: str = "claude_bridge_test"):
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

    cfg = root / "config.json"
    if not cfg.exists():
        import json
        cfg.write_text(json.dumps({
            "token": "x", "allowed_ids": [1],
            "tmux_session": tmux_session, "claude_path": "/usr/bin/claude"
        }))

    import importlib
    if "bot" in sys.modules:
        del sys.modules["bot"]
    b = importlib.import_module("bot")
    b.TMUX = tmux_session
    b.config.TMUX = tmux_session
    return b


bot = _load_bot()


def _tmux(rc: int, stdout: str = "") -> MagicMock:
    r = MagicMock()
    r.returncode = rc
    r.stdout = stdout
    return r


def _make_app(sent_messages: list):
    """Telegram app mock — send_message 호출을 리스트에 기록"""
    app = MagicMock()
    app.bot = MagicMock()

    async def fake_send(chat_id, text, **kwargs):
        sent_messages.append(text)

    app.bot.send_message = AsyncMock(side_effect=fake_send)
    return app


# ── A: tmux 세션 자체가 사라짐 (has-session 실패) ─────────────────────────────

def test_monitor_detects_missing_tmux_session():
    """has-session 실패 → 'tmux 세션이 사라졌습니다' 메시지 전송 + 루프 종료"""
    b = bot.Bridge()
    sent = []
    app = _make_app(sent)

    async def run():
        with patch.object(bot.tmux, "tmux_run", return_value=_tmux(1)), \
             patch.object(bot.parser, "extract_last_response", return_value=""):
            await b.monitor(app, chat_id=111)

    asyncio.run(run())

    assert b.running is False
    assert any("tmux 세션이 사라졌습니다" in m for m in sent)


def test_monitor_missing_tmux_sends_only_once():
    """단일 monitor 루프 내에서 has-session 실패 → dead_reported 로 한 번만 전송 후 루프 종료"""
    b = bot.Bridge()
    sent = []
    app = _make_app(sent)

    async def run():
        with patch.object(bot.tmux, "tmux_run", return_value=_tmux(1)), \
             patch.object(bot.parser, "extract_last_response", return_value=""):
            await b.monitor(app, chat_id=111)

    asyncio.run(run())

    dead_msgs = [m for m in sent if "tmux 세션이 사라졌습니다" in m]
    assert len(dead_msgs) == 1
    assert b.running is False
    assert b.dead_reported is True


def test_monitor_dead_reported_resets_on_new_monitor_call():
    """monitor() 재호출 시 dead_reported 초기화 → 새 세션에서 다시 감지 가능"""
    b = bot.Bridge()
    sent = []
    app = _make_app(sent)

    async def run():
        with patch.object(bot.tmux, "tmux_run", return_value=_tmux(1)), \
             patch.object(bot.parser, "extract_last_response", return_value=""):
            await b.monitor(app, chat_id=111)   # 첫 번째: 죽음 감지
            await b.monitor(app, chat_id=111)   # 두 번째: 새 모니터 세션, 다시 감지

    asyncio.run(run())

    dead_msgs = [m for m in sent if "tmux 세션이 사라졌습니다" in m]
    assert len(dead_msgs) == 2   # 각 monitor 세션에서 1번씩


# ── B: pane이 죽음 (is_alive == False, remain-on-exit) ────────────────────────

def test_monitor_detects_dead_pane():
    """is_alive_async() False → 'Claude 프로세스가 종료되었습니다' 메시지 전송 + 루프 종료"""
    b = bot.Bridge()
    sent = []
    app = _make_app(sent)

    last_output = "마지막 Claude 출력 내용"

    async def run():
        with patch.object(bot.tmux, "tmux_run_async", new_callable=AsyncMock, return_value=_tmux(0)), \
             patch.object(b, "is_alive_async", new_callable=AsyncMock, return_value=False), \
             patch.object(bot.tmux, "pane_output_async", new_callable=AsyncMock, return_value=last_output), \
             patch.object(bot.parser, "strip_ansi", side_effect=lambda x: x), \
             patch.object(bot.parser, "extract_last_response", return_value=""):
            await b.monitor(app, chat_id=111)

    asyncio.run(run())

    assert b.running is False
    assert any("Claude 프로세스가 종료되었습니다" in m for m in sent)


def test_monitor_dead_pane_includes_last_output():
    """pane 죽음 시 마지막 출력이 메시지에 포함됨"""
    b = bot.Bridge()
    sent = []
    app = _make_app(sent)

    async def run():
        with patch.object(bot.tmux, "tmux_run_async", new_callable=AsyncMock, return_value=_tmux(0)), \
             patch.object(b, "is_alive_async", new_callable=AsyncMock, return_value=False), \
             patch.object(bot.tmux, "pane_output_async", new_callable=AsyncMock,
                          return_value="error: segfault at line 99"), \
             patch.object(bot.parser, "strip_ansi", side_effect=lambda x: x), \
             patch.object(bot.parser, "extract_last_response", return_value=""):
            await b.monitor(app, chat_id=111)

    asyncio.run(run())

    assert any("segfault" in m for m in sent)


def test_monitor_dead_pane_reports_only_once():
    """pane 죽음 감지 후 dead_reported=True → 두 번째 루프 진입 시 재전송 없음"""
    b = bot.Bridge()
    sent = []
    app = _make_app(sent)

    async def run():
        with patch.object(bot.tmux, "tmux_run_async", new_callable=AsyncMock, return_value=_tmux(0)), \
             patch.object(b, "is_alive_async", new_callable=AsyncMock, return_value=False), \
             patch.object(bot.tmux, "pane_output_async", new_callable=AsyncMock, return_value="output"), \
             patch.object(bot.parser, "strip_ansi", side_effect=lambda x: x), \
             patch.object(bot.parser, "extract_last_response", return_value=""):
            await b.monitor(app, chat_id=111)

    asyncio.run(run())

    dead_msgs = [m for m in sent if "종료되었습니다" in m]
    assert len(dead_msgs) == 1


# ── C: 살아있다가 갑자기 죽는 시나리오 ───────────────────────────────────────

def test_monitor_alive_then_suddenly_dead():
    """처음 몇 틱은 정상, 그 다음 틱에서 pane 죽음 감지"""
    b = bot.Bridge()
    sent = []
    app = _make_app(sent)

    tick = [0]

    def fake_tmux(args):
        if "has-session" in args:
            return _tmux(0)          # tmux 세션은 항상 있음
        if "list-panes" in args:
            tick[0] += 1
            # 처음 2틱은 살아있음, 3번째부터 죽음
            stdout = "0" if tick[0] <= 2 else "1"
            return _tmux(0, stdout=stdout)
        return _tmux(0)

    iter_count = [0]

    async def fake_sleep(n):
        iter_count[0] += 1
        if iter_count[0] > 5:
            b.running = False   # 무한루프 방지

    async def run():
        with patch.object(bot.tmux, "tmux_run", side_effect=fake_tmux), \
             patch.object(bot.parser, "strip_ansi", side_effect=lambda x: x), \
             patch.object(bot.parser, "extract_last_response", return_value=""), \
             patch("asyncio.sleep", side_effect=fake_sleep):
            await b.monitor(app, chat_id=111)

    asyncio.run(run())

    assert any("종료되었습니다" in m for m in sent)


# ── D: 죽음 이후 락 상태 ──────────────────────────────────────────────────────

def test_lock_is_stale_after_tmux_dies(tmp_path):
    """tmux 죽은 후 _is_locked → stale 감지 → False + 파일 삭제"""
    lp = tmp_path / ".cb_lock_sessX"
    lp.write_text(f"{bot.TMUX}\n111")

    with patch.object(bot.session, "_lock_path", side_effect=lambda s: tmp_path / f".cb_lock_{s}"), \
         patch.object(bot.tmux, "tmux_run", return_value=_tmux(1)):   # tmux 없음
        locked = bot._is_locked("sessX")

    assert not locked
    assert not lp.exists()


def test_start_succeeds_after_tmux_dies(tmp_path):
    """tmux 죽어서 stale 정리 후 동일 세션 resume 가능"""
    lp = tmp_path / ".cb_lock_sessX"
    lp.write_text(f"{bot.TMUX}\n111")

    b = bot.Bridge()

    def fake_tmux(args):
        if "has-session" in args:
            return _tmux(1)   # 죽은 상태
        return _tmux(0)

    with patch.object(bot.session, "_lock_path", side_effect=lambda s: tmp_path / f".cb_lock_{s}"), \
         patch.object(bot.tmux, "tmux_run", side_effect=fake_tmux), \
         patch.object(b, "_spawn", return_value=True):
        ok = b.start("sessX", chat_id=111)

    assert ok
    assert lp.exists()   # 새 락 획득됨
    assert lp.read_text().splitlines()[0] == bot.TMUX


def test_find_sessions_shows_session_after_tmux_dies(tmp_path, monkeypatch):
    """tmux 죽으면 stale 락 해제 → find_sessions 목록에 다시 노출"""
    import time, json

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    sess_file = proj_dir / "sessX.jsonl"
    old_ts = time.time() - 120
    sess_file.write_text(
        json.dumps({"type": "user", "timestamp": "2020-01-01T00:00:00Z",
                    "message": {"content": "이전 작업 내용입니다 충분히 긴 메시지로 15자 필터 통과"}}) + "\n"
    )
    import os
    os.utime(sess_file, (old_ts, old_ts))

    # 락 파일 존재 (죽기 전 상태)
    lp = tmp_path / ".cb_lock_sessX"
    lp.write_text(f"{bot.TMUX}\n111")

    monkeypatch.setattr(bot.config, "PROJECTS", tmp_path)

    # tmux 세션 죽음 → stale → find_sessions에 노출
    with patch.object(bot.session, "_lock_path", side_effect=lambda s: tmp_path / f".cb_lock_{s}"), \
         patch.object(bot.tmux, "tmux_run", return_value=_tmux(1)):
        sessions = bot.find_sessions()

    ids = [s["id"] for s in sessions]
    assert "sessX" in ids


# ── E: 죽음 감지 후 재시작 시나리오 (시퀀스) ─────────────────────────────────

def test_tmux_death_to_restart_sequence(tmp_path):
    """tmux 갑자기 죽음 → 알림 → /start → 새 세션 시작까지 전체 흐름"""
    lp = tmp_path / ".cb_lock_sessY"
    lp.write_text(f"{bot.TMUX}\n111")

    b = bot.Bridge()
    b.current_session_id = "sessY"
    sent = []
    app = _make_app(sent)

    # 1) monitor 루프: has-session 실패 → 종료 알림
    async def run_monitor():
        with patch.object(bot.tmux, "tmux_run", return_value=_tmux(1)), \
             patch.object(bot.parser, "extract_last_response", return_value=""):
            await b.monitor(app, chat_id=111)

    asyncio.run(run_monitor())
    assert any("사라졌습니다" in m for m in sent)
    assert b.running is False
    # 락 파일은 monitor 루프가 해제하지 않음 — stop()을 통해서만 해제
    assert lp.exists()

    # 2) /start → 신규 세션 (stale 락 자동 정리 후 resume 가능)
    with patch.object(bot.session, "_lock_path", side_effect=lambda s: tmp_path / f".cb_lock_{s}"), \
         patch.object(bot.tmux, "tmux_run", return_value=_tmux(1)), \
         patch.object(b, "_spawn", return_value=True):
        # has-session=1 이므로 stale → 정리 후 acquire
        ok = b.start("sessY", chat_id=111)

    assert ok


def test_tmux_death_lock_released_only_via_stop_or_stale(tmp_path):
    """monitor가 죽음을 감지해도 락 파일은 stop() 또는 stale 감지로만 해제됨"""
    lp = tmp_path / ".cb_lock_sessZ"
    lp.write_text(f"{bot.TMUX}\n111")

    b = bot.Bridge()
    b.current_session_id = "sessZ"
    app = _make_app([])

    async def run():
        with patch.object(bot.tmux, "tmux_run", return_value=_tmux(1)), \
             patch.object(bot.parser, "extract_last_response", return_value=""):
            await b.monitor(app, chat_id=111)

    asyncio.run(run())

    # monitor 루프만으론 락 해제 안 됨
    assert lp.exists(), "monitor 루프는 락을 직접 해제하지 않음 (stop/stale에 위임)"
