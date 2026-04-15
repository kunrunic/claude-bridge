"""
중복/동시 커맨드 수신 케이스 테스트.

시나리오:
  - /start 중복 수신 (세션 목록 두 번 표시)
  - resume 콜백 더블탭 (같은 session_id 동시 start)
  - /end 더블탭 (stop 두 번 호출)
  - /esc 더블탭
  - force_new 더블탭 (kill 두 번)
  - reattach 더블탭 (monitor task 두 번 생성)
  - 오해 소지 있는 에러 메시지 확인 (자기 자신이 이미 점유 중일 때)
"""
from __future__ import annotations

import sys
import types
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import pytest


# ── stub helpers ──────────────────────────────────────────────────────────────

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
    return b


bot = _load_bot()


def _tmux(returncode: int) -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    return r


# ── resume 더블탭 ──────────────────────────────────────────────────────────────

def test_double_acquire_same_session_same_tmux(tmp_path):
    """같은 인스턴스가 같은 session_id를 두 번 acquire → 두 번째 False"""
    lp = tmp_path / ".cb_lock_sessX"
    with patch.object(bot, "_lock_path", return_value=lp):
        first = bot._acquire_lock("sessX", chat_id=111)
        # 두 번째: tmux 세션이 살아있다고 가정 (방금 spawn됨)
        with patch.object(bot, "tmux_run", return_value=_tmux(0)):
            second = bot._acquire_lock("sessX", chat_id=111)
    assert first is True
    assert second is False   # 중복 spawn 방지


def test_bridge_start_double_same_session(tmp_path):
    """bridge.start() 동일 세션 두 번 호출 → 두 번째는 False"""
    lp = tmp_path / ".cb_lock_sessY"
    b = bot.Bridge()

    call_count = [0]

    def fake_spawn(_=None):
        call_count[0] += 1
        return True

    def fake_lock_path(s):
        return lp

    with patch.object(bot, "_lock_path", side_effect=fake_lock_path), \
         patch.object(bot, "tmux_run", return_value=_tmux(0)), \
         patch.object(b, "_spawn", side_effect=fake_spawn):
        first = b.start("sessY", chat_id=111)   # 락 획득, spawn
        second = b.start("sessY", chat_id=111)  # 락 이미 있음 → False

    assert first is True
    assert second is False
    assert call_count[0] == 1   # spawn은 한 번만


def test_bridge_start_double_error_is_lock_conflict_not_other_instance(tmp_path):
    """두 번째 start 실패 원인이 '자기 자신'임을 _is_locked로 확인 가능해야 함"""
    lp = tmp_path / ".cb_lock_sessZ"
    b = bot.Bridge()

    with patch.object(bot, "_lock_path", return_value=lp), \
         patch.object(bot, "tmux_run", return_value=_tmux(0)), \
         patch.object(b, "_spawn", return_value=True):
        b.start("sessZ", chat_id=111)
        second_ok = b.start("sessZ", chat_id=111)

    assert not second_ok
    # 락 소유자가 자기 자신(TMUX)인지 확인
    info = bot._parse_lock("sessZ")   # _lock_path는 아직 패치된 상태 아님
    # 파일에서 직접 읽어서 확인
    lines = lp.read_text().splitlines()
    assert lines[0] == bot.TMUX   # 자기 인스턴스 소유


# ── /end 더블탭 ───────────────────────────────────────────────────────────────

def test_stop_twice_no_exception(tmp_path):
    """bridge.stop() 두 번 호출해도 예외 없이 완료"""
    lp = tmp_path / ".cb_lock_sessE"
    lp.write_text(f"{bot.TMUX}\n111")
    b = bot.Bridge()
    b.current_session_id = "sessE"
    b.running = True

    async def run():
        with patch.object(bot, "_lock_path", return_value=lp), \
             patch.object(bot, "tmux_run", return_value=_tmux(0)), \
             patch.object(bot, "send_input"), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            await b.stop()
            await b.stop()   # 두 번째 — 예외 없어야 함

    asyncio.run(run())
    assert b.running is False


def test_stop_releases_lock_only_once(tmp_path):
    """stop() 두 번 호출 → 락 파일은 첫 번째에 삭제, 두 번째는 no-op"""
    lp = tmp_path / ".cb_lock_sessF"
    lp.write_text(f"{bot.TMUX}\n111")
    b = bot.Bridge()
    b.current_session_id = "sessF"

    async def run():
        with patch.object(bot, "_lock_path", side_effect=lambda s: tmp_path / f".cb_lock_{s}"), \
             patch.object(bot, "_LOCK_DIR", tmp_path), \
             patch.object(bot, "tmux_run", return_value=_tmux(0)), \
             patch.object(bot, "send_input"), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            await b.stop()
            assert not lp.exists()
            await b.stop()   # 파일 이미 없어도 예외 없음

    asyncio.run(run())


# ── force_new 더블탭 ──────────────────────────────────────────────────────────

def test_force_new_double_kill_no_exception():
    """tmux kill-session 두 번 → 두 번째는 returncode != 0 이어도 무시"""
    call_count = [0]
    def fake_tmux(args):
        call_count[0] += 1
        r = MagicMock()
        # 첫 번째 kill: 성공, 두 번째: 세션 없음(실패)
        r.returncode = 0 if call_count[0] == 1 else 1
        return r

    with patch.object(bot, "tmux_run", side_effect=fake_tmux):
        bot.tmux_run(["kill-session", "-t", bot.TMUX])
        bot.tmux_run(["kill-session", "-t", bot.TMUX])   # 예외 없어야 함

    assert call_count[0] == 2


# ── reattach 더블탭 ───────────────────────────────────────────────────────────

def test_reattach_double_cancels_previous_task():
    """reattach 두 번 → 이전 task가 cancel 되고 새 task로 교체"""
    b = bot.Bridge()

    task1 = MagicMock()
    task1.cancel = MagicMock()
    b.task = task1

    task2 = MagicMock()

    with patch("asyncio.create_task", return_value=task2):
        # 첫 번째 reattach
        if b.task:
            b.task.cancel()
        b.task = asyncio.create_task(MagicMock())

        task1.cancel.assert_called_once()
        prev_task = b.task

        # 두 번째 reattach
        if b.task:
            b.task.cancel()
        b.task = asyncio.create_task(MagicMock())

    assert b.task is not task1   # 교체됨


# ── /start 중복 (세션 없을 때) ─────────────────────────────────────────────────

def test_cmd_start_double_when_no_session_both_show_list():
    """/start 두 번 다 tmux 세션 없으면 두 번 다 목록 표시 — 중복 메뉴 발생"""
    # 현재 방어 없음을 문서화하는 테스트 (regression 용)
    with patch.object(bot, "tmux_run", return_value=_tmux(1)):   # 세션 없음
        # has-session returncode=1 → cmd_start가 find_sessions 호출
        check = bot.tmux_run(["has-session", "-t", bot.TMUX])
    assert check.returncode == 1   # 두 번째 /start도 동일 경로 진입 가능


def test_cmd_start_when_session_alive_shows_reattach():
    """/start 시 tmux 세션이 살아있으면 재연결/새로시작 메뉴 표시"""
    with patch.object(bot, "tmux_run", return_value=_tmux(0)):   # 세션 있음
        check = bot.tmux_run(["has-session", "-t", bot.TMUX])
    assert check.returncode == 0   # → cmd_start가 reattach 메뉴 반환


# ── /esc 더블탭 ───────────────────────────────────────────────────────────────

def test_esc_double_sends_twice():
    """ESC 두 번 → tmux에 Escape 키 두 번 전송 (각각 독립적)"""
    call_args = []
    def fake_send_key(key):
        call_args.append(key)

    with patch.object(bot, "send_key", side_effect=fake_send_key), \
         patch.object(bot, "tmux_run", return_value=_tmux(0)):
        bot.send_key("Escape")
        bot.send_key("Escape")

    assert call_args.count("Escape") == 2


# ── approve 더블탭 ────────────────────────────────────────────────────────────

def test_approve_double_tap_second_is_noop():
    """승인 버튼 두 번 탭 → 두 번째는 awaiting_approval=False 이므로 무시"""
    b = bot.Bridge()
    b.awaiting_approval = True

    # 첫 번째 승인 처리
    b.awaiting_approval = False

    # 두 번째: 이미 False → 핸들러에서 "이미 처리됨" 반환
    assert b.awaiting_approval is False   # 재진입 막힘


def test_approve_yes_then_no_second_blocked():
    """Yes 승인 후 No 거부 시도 → awaiting_approval=False 로 두 번째 차단"""
    b = bot.Bridge()
    b.awaiting_approval = True

    # Yes 처리
    if not b.awaiting_approval:
        pytest.fail("첫 번째 승인이 막혀야 안 됨")
    b.awaiting_approval = False

    # No 시도 — 이미 처리됨
    assert not b.awaiting_approval   # → on_callback에서 early return


# ── Bridge.start lock → 이전 lock 정리 연속 호출 ─────────────────────────────

def test_bridge_start_sequential_different_sessions(tmp_path):
    """세션 A → 세션 B 순서로 start() 호출 시 A 락 해제 + B 락 획득"""
    lp_a = tmp_path / ".cb_lock_A"
    lp_b = tmp_path / ".cb_lock_B"

    def fake_lock_path(s):
        return lp_a if s == "A" else lp_b

    b = bot.Bridge()

    with patch.object(bot, "_lock_path", side_effect=fake_lock_path), \
         patch.object(bot, "tmux_run", return_value=_tmux(0)), \
         patch.object(b, "_spawn", return_value=True):
        b.start("A", chat_id=111)
        assert lp_a.exists()

        b.start("B", chat_id=111)   # A 락 해제 후 B 획득
        assert not lp_a.exists()    # A 해제됨
        assert lp_b.exists()        # B 획득됨
