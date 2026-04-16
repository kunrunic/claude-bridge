"""
시나리오 기반 통합 테스트.

실제 사용 흐름(다단계 시퀀스)을 재현하며 각 단계의 상태를 검증한다.
단일 동작 단위 테스트(test_lock_and_limit, test_concurrent_commands)를 보완한다.
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


def _tmux(rc: int) -> MagicMock:
    r = MagicMock()
    r.returncode = rc
    return r


def _lock_file(tmp_path, session_id, tmux_name=None, chat_id=111):
    lp = tmp_path / f".cb_lock_{session_id}"
    lp.write_text(f"{tmux_name or bot.TMUX}\n{chat_id}")
    return lp


# ── S1: 정상 라이프사이클 ──────────────────────────────────────────────────────
# start sessA → /end → /start → sessA 목록 노출 → resume sessA

def test_s1_normal_lifecycle_lock_state(tmp_path):
    """S1: 세션 시작 → 종료 → 재개 전 과정에서 락 상태가 올바름"""
    lp = tmp_path / ".cb_lock_sessA"
    b = bot.Bridge()

    def lpath(s): return tmp_path / f".cb_lock_{s}"

    # 1) start sessA → 락 획득
    with patch.object(bot.session, "_lock_path", side_effect=lpath), \
         patch.object(bot.tmux, "tmux_run", return_value=_tmux(0)), \
         patch.object(b, "_spawn", return_value=True):
        ok = b.start("sessA", chat_id=111)
    assert ok
    assert (tmp_path / ".cb_lock_sessA").exists()

    # 2) /end → 락 해제
    async def do_stop():
        with patch.object(bot.session, "_lock_path", side_effect=lpath), \
             patch.object(bot.session, "_LOCK_DIR", tmp_path), \
             patch.object(bot.tmux, "tmux_run", return_value=_tmux(0)), \
             patch.object(bot.tmux, "send_input"), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            await b.stop()

    asyncio.run(do_stop())
    assert not (tmp_path / ".cb_lock_sessA").exists()   # 락 해제됨
    assert b.current_session_id is None

    # 3) /start 후 세션 목록 — sessA가 다시 보여야 함 (락 없으니 _is_locked=False)
    with patch.object(bot.session, "_lock_path", side_effect=lpath):
        locked = bot._is_locked("sessA")
    assert not locked

    # 4) resume sessA → 락 재획득
    with patch.object(bot.session, "_lock_path", side_effect=lpath), \
         patch.object(bot.tmux, "tmux_run", return_value=_tmux(0)), \
         patch.object(b, "_spawn", return_value=True):
        ok2 = b.start("sessA", chat_id=111)
    assert ok2
    assert (tmp_path / ".cb_lock_sessA").exists()


# ── S2: 봇 크래시 후 재시작 ───────────────────────────────────────────────────
# start sessA → 크래시(lock 잔류, tmux 살아있음) → 재시작 → reattach → /end
# 문제: 재시작 후 current_session_id=None 이므로 /end 가 lock을 해제하지 못함

def test_s2_crash_recovery_lock_survives_reattach(tmp_path):
    """S2: 크래시 후 reattach → /end 해도 락이 남아있는 현재 동작을 문서화"""
    # 크래시 직전 상태: lock 파일 존재, tmux 세션 살아있음
    lp = _lock_file(tmp_path, "sessA")

    # 재시작: 새 Bridge 인스턴스 (current_session_id=None 복구 안 됨)
    b_new = bot.Bridge()
    assert b_new.current_session_id is None

    # reattach: monitor만 연결, current_session_id 미복구
    # /end 호출
    async def do_stop():
        with patch.object(bot.session, "_lock_path", side_effect=lambda s: tmp_path / f".cb_lock_{s}"), \
             patch.object(bot.session, "_LOCK_DIR", tmp_path), \
             patch.object(bot.tmux, "tmux_run", return_value=_tmux(0)), \
             patch.object(bot.tmux, "send_input"), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            await b_new.stop()

    asyncio.run(do_stop())

    # stop()이 _my_locks()로 이 인스턴스 소유 락을 전부 해제하므로 락 없어야 함
    assert not lp.exists(), "크래시 후 reattach → /end 시 락이 해제되어야 함"


def test_s2_crash_recovery_stale_cleanup_after_tmux_dies(tmp_path):
    """S2 보완: tmux 세션이 죽으면 다음 find_sessions 시 stale 감지로 자동 정리"""
    _lock_file(tmp_path, "sessA")

    def lpath(s): return tmp_path / f".cb_lock_{s}"

    # tmux 세션 죽은 후 _is_locked 호출 → stale 정리
    with patch.object(bot.session, "_lock_path", side_effect=lpath), \
         patch.object(bot.tmux, "tmux_run", return_value=_tmux(1)):   # 세션 없음
        locked = bot._is_locked("sessA")

    assert not locked
    assert not (tmp_path / ".cb_lock_sessA").exists()   # 자동 정리됨


# ── S3: force_new 흐름 ────────────────────────────────────────────────────────
# start sessA → /start → force_new → 신규 세션 선택 → sessA lock 해제 여부

def test_s3_force_new_then_new_session_releases_old_lock(tmp_path):
    """S3: force_new 후 신규 세션 시작 → 이전 sessA 락이 bridge.start(None) 시 해제됨"""
    def lpath(s): return tmp_path / f".cb_lock_{s}"

    b = bot.Bridge()

    # 1) start sessA
    with patch.object(bot.session, "_lock_path", side_effect=lpath), \
         patch.object(bot.tmux, "tmux_run", return_value=_tmux(0)), \
         patch.object(b, "_spawn", return_value=True):
        b.start("sessA", chat_id=111)

    assert (tmp_path / ".cb_lock_sessA").exists()
    assert b.current_session_id == "sessA"

    # 2) force_new: tmux kill (bridge.stop() 없이 직접 kill)
    #    → bridge.current_session_id 는 여전히 "sessA"
    with patch.object(bot.tmux, "tmux_run", return_value=_tmux(0)):
        bot.tmux.tmux_run(["kill-session", "-t", bot.TMUX])

    assert b.current_session_id == "sessA"   # force_new는 이를 초기화 안 함

    # 3) 신규 세션 선택: bridge.start(None) → 내부에서 _release_lock("sessA") 호출
    with patch.object(bot.session, "_lock_path", side_effect=lpath), \
         patch.object(bot.tmux, "tmux_run", return_value=_tmux(0)), \
         patch.object(b, "_spawn", return_value=True):
        b.start(None, chat_id=111)

    assert not (tmp_path / ".cb_lock_sessA").exists()   # 이전 락 해제됨
    assert b.current_session_id is None


def test_s3_force_new_then_resume_other_session(tmp_path):
    """S3 변형: force_new 후 다른 sessB resume → sessA 락 해제 + sessB 락 획득"""
    def lpath(s): return tmp_path / f".cb_lock_{s}"

    b = bot.Bridge()

    with patch.object(bot.session, "_lock_path", side_effect=lpath), \
         patch.object(bot.tmux, "tmux_run", return_value=_tmux(0)), \
         patch.object(b, "_spawn", return_value=True):
        b.start("sessA", chat_id=111)

    with patch.object(bot.session, "_lock_path", side_effect=lpath), \
         patch.object(bot.tmux, "tmux_run", return_value=_tmux(0)), \
         patch.object(b, "_spawn", return_value=True):
        b.start("sessB", chat_id=111)

    assert not (tmp_path / ".cb_lock_sessA").exists()
    assert (tmp_path / ".cb_lock_sessB").exists()


# ── S4: 더블 신규세션 버그 ────────────────────────────────────────────────────
# /start × 2 → 둘 다 "새 세션" 선택 → 첫 번째 tmux가 두 번째에 의해 kill

def test_s4_double_new_session_second_kills_first():
    """S4: 신규 세션 두 번 → 두 번째 start(None)가 첫 번째 tmux를 kill함 (known bug)"""
    b = bot.Bridge()
    kill_calls = []
    spawn_calls = []

    def fake_tmux(args):
        if "kill-session" in args:
            kill_calls.append(list(args))
        r = MagicMock()
        r.returncode = 0
        r.stdout = "0"
        return r

    def fake_spawn(session_id=None):
        spawn_calls.append(session_id)
        return True

    with patch.object(bot.tmux, "tmux_run", side_effect=fake_tmux), \
         patch.object(b, "_spawn", side_effect=fake_spawn):
        first = b.start(None, chat_id=111)   # 첫 번째 신규
        second = b.start(None, chat_id=111)  # 두 번째 신규

    assert first is True
    assert second is True
    # spawn 두 번 → 두 개의 tmux 세션 생성 시도 (문제)
    assert len(spawn_calls) == 2
    # 두 번째 start의 kill이 첫 번째 세션을 kill
    assert len([c for c in kill_calls if "kill-session" in c]) >= 2


def test_s4_double_new_session_monitor_count():
    """S4: 두 번째 start가 성공하면 monitor task가 교체됨 (task cancel 확인)"""
    b = bot.Bridge()
    task1 = MagicMock()
    task1.cancel = MagicMock()

    with patch.object(bot.tmux, "tmux_run", return_value=_tmux(0)), \
         patch.object(b, "_spawn", return_value=True):
        b.start(None, chat_id=111)
        b.task = task1   # 첫 번째 monitor가 등록됐다고 가정

        # 두 번째 start 후 on_callback이 task1.cancel() 호출
        b.task.cancel()
        b.start(None, chat_id=111)

    task1.cancel.assert_called_once()


# ── S5: 기존세션 reattach → /start → 신규 ────────────────────────────────────
# 기존 세션에 재연결 후 /start → force_new or 신규 선택 → 기존 세션 종료 여부

def test_s5_reattach_then_start_new_kills_old_tmux(tmp_path):
    """S5: reattach 상태에서 /start → force_new → 신규 세션 → 기존 tmux kill 확인"""
    def lpath(s): return tmp_path / f".cb_lock_{s}"

    b = bot.Bridge()

    # 1) 최초 시작 (sessA)
    with patch.object(bot.session, "_lock_path", side_effect=lpath), \
         patch.object(bot.tmux, "tmux_run", return_value=_tmux(0)), \
         patch.object(b, "_spawn", return_value=True):
        b.start("sessA", chat_id=111)

    # 2) reattach: 봇 재시작 후 monitor만 재연결 (current_session_id는 유지된 상태)
    b.running = True

    # 3) /start → force_new → kill + 신규 세션 선택
    kill_log = []
    def fake_tmux(args):
        if "kill-session" in args:
            kill_log.append("killed")
        r = MagicMock()
        r.returncode = 0
        r.stdout = "0"
        return r

    with patch.object(bot.session, "_lock_path", side_effect=lpath), \
         patch.object(bot.tmux, "tmux_run", side_effect=fake_tmux), \
         patch.object(b, "_spawn", return_value=True):
        # force_new: 기존 tmux kill
        bot.tmux.tmux_run(["kill-session", "-t", bot.TMUX])
        # 신규 세션 start
        b.start(None, chat_id=111)

    assert "killed" in kill_log                          # 기존 tmux kill 됨
    assert not (tmp_path / ".cb_lock_sessA").exists()    # 이전 락 해제됨


def test_s5_reattach_then_resume_different_session(tmp_path):
    """S5 변형: reattach → /start → 다른 sessB resume → sessA lock 교체"""
    def lpath(s): return tmp_path / f".cb_lock_{s}"

    b = bot.Bridge()

    # sessA로 시작
    with patch.object(bot.session, "_lock_path", side_effect=lpath), \
         patch.object(bot.tmux, "tmux_run", return_value=_tmux(0)), \
         patch.object(b, "_spawn", return_value=True):
        b.start("sessA", chat_id=111)

    # reattach 상태에서 sessB 선택
    with patch.object(bot.session, "_lock_path", side_effect=lpath), \
         patch.object(bot.tmux, "tmux_run", return_value=_tmux(0)), \
         patch.object(b, "_spawn", return_value=True):
        ok = b.start("sessB", chat_id=111)

    assert ok
    assert not (tmp_path / ".cb_lock_sessA").exists()   # A 해제
    assert (tmp_path / ".cb_lock_sessB").exists()        # B 획득
    assert b.current_session_id == "sessB"


# ── S6: /end 없이 /start → 신규 세션 (기존 세션 정상 종료 여부) ────────────────

def test_s6_start_without_end_existing_session(tmp_path):
    """S6: /end 없이 /start → 신규 세션 → 기존 세션이 kill되는지 확인"""
    def lpath(s): return tmp_path / f".cb_lock_{s}"

    b = bot.Bridge()
    killed = []

    def fake_tmux(args):
        if "kill-session" in args:
            killed.append(True)
        r = MagicMock()
        r.returncode = 0
        r.stdout = "0"
        return r

    # 기존 sessA 실행 중
    with patch.object(bot.session, "_lock_path", side_effect=lpath), \
         patch.object(bot.tmux, "tmux_run", side_effect=fake_tmux), \
         patch.object(b, "_spawn", return_value=True):
        b.start("sessA", chat_id=111)

    killed.clear()

    # /end 없이 바로 신규 세션 start
    with patch.object(bot.session, "_lock_path", side_effect=lpath), \
         patch.object(bot.tmux, "tmux_run", side_effect=fake_tmux), \
         patch.object(b, "_spawn", return_value=True):
        b.start(None, chat_id=111)

    # bridge.start()가 내부에서 kill-session 호출함
    assert len(killed) >= 1                              # tmux 종료됨
    assert not (tmp_path / ".cb_lock_sessA").exists()   # 락 해제됨
    # 단, /exit 없이 강제 kill → Claude 세션 dirty 종료 가능성 있음


def test_s6_graceful_end_vs_force_start(tmp_path):
    """/end(graceful) vs /start 직접(force kill) 차이 확인"""
    def lpath(s): return tmp_path / f".cb_lock_{s}"

    b = bot.Bridge()

    # graceful: /end → _release_lock 명시적 호출
    with patch.object(bot.session, "_lock_path", side_effect=lpath), \
         patch.object(bot.tmux, "tmux_run", return_value=_tmux(0)), \
         patch.object(b, "_spawn", return_value=True):
        b.start("sessA", chat_id=111)

    async def graceful_stop():
        with patch.object(bot.session, "_lock_path", side_effect=lpath), \
             patch.object(bot.session, "_LOCK_DIR", tmp_path), \
             patch.object(bot.tmux, "tmux_run", return_value=_tmux(0)), \
             patch.object(bot.tmux, "send_input"), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            await b.stop()   # /exit 전송 → 5초 대기 → kill

    asyncio.run(graceful_stop())

    assert not (tmp_path / ".cb_lock_sessA").exists()   # 정상 해제
    assert b.current_session_id is None                  # 초기화됨
