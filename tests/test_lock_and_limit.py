"""
세션 락 + 사용량 한도 감지 테스트.

신규 기능:
  - _parse_lock / _acquire_lock / _release_lock / _is_locked / _my_locks
  - chat_id 소유권 검증
  - stale 락 자동 정리
  - LIMIT_RE 패턴 매칭
  - find_sessions 에서 락 걸린 세션 제외
  - Bridge.start() 락 획득/실패
"""
from __future__ import annotations

import sys
import types
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile
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
    bot = importlib.import_module("bot")
    # 테스트용 TMUX 이름 주입
    bot.TMUX = tmux_session
    bot.config.TMUX = tmux_session
    return bot


bot = _load_bot()


def _dead_tmux() -> MagicMock:
    """tmux has-session → 세션 없음(returncode=1)"""
    r = MagicMock()
    r.returncode = 1
    return r


def _alive_tmux() -> MagicMock:
    """tmux has-session → 세션 있음(returncode=0)"""
    r = MagicMock()
    r.returncode = 0
    return r


# ── _parse_lock ────────────────────────────────────────────────────────────────

def test_parse_lock_valid(tmp_path):
    lp = tmp_path / ".cb_lock_abc"
    lp.write_text("claude_bridge\n12345678")
    with patch.object(bot.session, "_lock_path", return_value=lp):
        result = bot._parse_lock("abc")
    assert result == ("claude_bridge", 12345678)


def test_parse_lock_missing(tmp_path):
    with patch.object(bot.session, "_lock_path", return_value=tmp_path / "nonexistent"):
        assert bot._parse_lock("abc") is None


def test_parse_lock_malformed(tmp_path):
    lp = tmp_path / ".cb_lock_abc"
    lp.write_text("claude_bridge")   # chat_id 행 없음
    with patch.object(bot.session, "_lock_path", return_value=lp):
        assert bot._parse_lock("abc") is None


def test_parse_lock_bad_chat_id(tmp_path):
    lp = tmp_path / ".cb_lock_abc"
    lp.write_text("claude_bridge\nnot_a_number")
    with patch.object(bot.session, "_lock_path", return_value=lp):
        assert bot._parse_lock("abc") is None


# ── _acquire_lock ──────────────────────────────────────────────────────────────

def test_acquire_lock_success(tmp_path):
    lp = tmp_path / ".cb_lock_sess1"
    with patch.object(bot.session, "_lock_path", return_value=lp):
        assert bot._acquire_lock("sess1", chat_id=111) is True
    assert lp.exists()
    lines = lp.read_text().splitlines()
    assert lines[0] == bot.TMUX
    assert lines[1] == "111"


def test_acquire_lock_duplicate_fails(tmp_path):
    lp = tmp_path / ".cb_lock_sess2"
    lp.write_text(f"{bot.TMUX}\n999")    # 다른 chat_id가 이미 소유
    with patch.object(bot.session, "_lock_path", return_value=lp), \
         patch.object(bot.tmux, "tmux_run", return_value=_alive_tmux()):
        assert bot._acquire_lock("sess2", chat_id=111) is False


def test_acquire_lock_stale_cleanup(tmp_path):
    """락 파일이 있어도 tmux 세션이 죽어있으면 정리 후 획득"""
    lp = tmp_path / ".cb_lock_sess3"
    lp.write_text("other_tmux\n999")
    with patch.object(bot.session, "_lock_path", return_value=lp), \
         patch.object(bot.tmux, "tmux_run", return_value=_dead_tmux()):
        assert bot._acquire_lock("sess3", chat_id=111) is True
    assert lp.read_text().splitlines()[0] == bot.TMUX


def test_acquire_lock_dir_error_allows(tmp_path):
    """락 디렉토리 오류 시 방어적으로 True 반환"""
    ro = tmp_path / "readonly"
    ro.mkdir()
    lp = ro / ".cb_lock_sess4"
    # open("x") 가 PermissionError 발생하도록 패치
    with patch.object(bot.session, "_lock_path", return_value=lp), \
         patch("builtins.open", side_effect=PermissionError):
        assert bot._acquire_lock("sess4", chat_id=111) is True


# ── _release_lock ──────────────────────────────────────────────────────────────

def test_release_lock_own(tmp_path):
    lp = tmp_path / ".cb_lock_sess5"
    lp.write_text(f"{bot.TMUX}\n111")
    with patch.object(bot.session, "_lock_path", return_value=lp):
        bot._release_lock("sess5")
    assert not lp.exists()


def test_release_lock_other_tmux_not_deleted(tmp_path):
    """다른 인스턴스 소유 락은 삭제하지 않음"""
    lp = tmp_path / ".cb_lock_sess6"
    lp.write_text("other_tmux\n111")
    with patch.object(bot.session, "_lock_path", return_value=lp):
        bot._release_lock("sess6")
    assert lp.exists()


def test_release_lock_none_session():
    """session_id=None 이면 아무것도 안 함"""
    bot._release_lock(None)   # 예외 없이 통과


def test_release_lock_missing_file(tmp_path):
    """락 파일이 이미 없어도 예외 없이 통과"""
    with patch.object(bot.session, "_lock_path", return_value=tmp_path / "nonexistent"):
        bot._release_lock("no_sess")


# ── _is_locked ─────────────────────────────────────────────────────────────────

def test_is_locked_no_file(tmp_path):
    with patch.object(bot.session, "_lock_path", return_value=tmp_path / "nonexistent"):
        assert bot._is_locked("sess7") is False


def test_is_locked_alive(tmp_path):
    lp = tmp_path / ".cb_lock_sess8"
    lp.write_text(f"{bot.TMUX}\n111")
    with patch.object(bot.session, "_lock_path", return_value=lp), \
         patch.object(bot.tmux, "tmux_run", return_value=_alive_tmux()):
        assert bot._is_locked("sess8") is True


def test_is_locked_dead_cleans_up(tmp_path):
    """tmux 세션 없으면 stale → False + 파일 삭제"""
    lp = tmp_path / ".cb_lock_sess9"
    lp.write_text("other_tmux\n999")
    with patch.object(bot.session, "_lock_path", return_value=lp), \
         patch.object(bot.tmux, "tmux_run", return_value=_dead_tmux()):
        assert bot._is_locked("sess9") is False
    assert not lp.exists()


def test_is_locked_malformed_file_cleans_up(tmp_path):
    """파싱 실패한 락 파일 → False + 파일 삭제"""
    lp = tmp_path / ".cb_lock_sess10"
    lp.write_text("garbage")
    with patch.object(bot.session, "_lock_path", return_value=lp):
        assert bot._is_locked("sess10") is False
    assert not lp.exists()


# ── _my_locks ──────────────────────────────────────────────────────────────────

def test_my_locks_empty(tmp_path):
    with patch.object(bot.session, "_LOCK_DIR", tmp_path):
        assert bot._my_locks() == []


def test_my_locks_own(tmp_path):
    (tmp_path / f".cb_lock_sessA").write_text(f"{bot.TMUX}\n111")
    with patch.object(bot.session, "_LOCK_DIR", tmp_path), \
         patch.object(bot.session, "_lock_path", side_effect=lambda s: tmp_path / f".cb_lock_{s}"):
        locks = bot._my_locks()
    assert len(locks) == 1
    assert locks[0] == ("sessA", 111)


def test_my_locks_other_instance_excluded(tmp_path):
    (tmp_path / ".cb_lock_sessB").write_text("other_tmux\n111")
    with patch.object(bot.session, "_LOCK_DIR", tmp_path), \
         patch.object(bot.session, "_lock_path", side_effect=lambda s: tmp_path / f".cb_lock_{s}"):
        locks = bot._my_locks()
    assert locks == []


def test_my_locks_mixed(tmp_path):
    """본인 락 1개 + 타인 인스턴스 락 1개 → 본인 것만"""
    (tmp_path / ".cb_lock_mine").write_text(f"{bot.TMUX}\n111")
    (tmp_path / ".cb_lock_other").write_text("other_tmux\n222")
    with patch.object(bot.session, "_LOCK_DIR", tmp_path), \
         patch.object(bot.session, "_lock_path", side_effect=lambda s: tmp_path / f".cb_lock_{s}"):
        locks = bot._my_locks()
    assert len(locks) == 1
    assert locks[0][0] == "mine"


# ── chat_id 소유권 ─────────────────────────────────────────────────────────────

def test_lock_stores_chat_id(tmp_path):
    lp = tmp_path / ".cb_lock_sessC"
    with patch.object(bot.session, "_lock_path", return_value=lp):
        bot._acquire_lock("sessC", chat_id=987654321)
    info = bot._parse_lock.__wrapped__("sessC") if hasattr(bot._parse_lock, "__wrapped__") else None
    lines = lp.read_text().splitlines()
    assert lines[1] == "987654321"


def test_acquire_lock_different_chat_id_same_tmux(tmp_path):
    """같은 tmux지만 다른 chat_id → 이미 내 tmux가 점유 중이므로 stale 아님 → False"""
    lp = tmp_path / ".cb_lock_sessD"
    lp.write_text(f"{bot.TMUX}\n111")   # 이미 내 tmux(다른 chat_id)가 점유
    with patch.object(bot.session, "_lock_path", return_value=lp), \
         patch.object(bot.tmux, "tmux_run", return_value=_alive_tmux()):
        assert bot._acquire_lock("sessD", chat_id=222) is False


# ── LIMIT_RE ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "You've hit your limit · resets 5pm (Asia/Seoul)",
    "you've hit your limit",
    "hit your daily limit",
    "hit your limit",
])
def test_limit_re_matches(text):
    assert bot.LIMIT_RE.search(text) is not None


@pytest.mark.parametrize("text", [
    "Running bash command",
    "esc to interrupt",
    "Do you want to proceed",
    "Claude Code v1.0",
])
def test_limit_re_no_match(text):
    assert bot.LIMIT_RE.search(text) is None


def test_limit_reset_time_extraction():
    """resets 시각 파싱이 올바르게 동작하는지"""
    import re
    text = "You've hit your limit · resets 5pm (Asia/Seoul)"
    m = re.search(r"resets\s+(\d+(?::\d+)?(?:am|pm)?)\s*\(([^)]+)\)", text, re.IGNORECASE)
    assert m is not None
    assert "5pm" in m.group(1)
    assert "Asia/Seoul" in m.group(2)


# ── Bridge.start 락 통합 ───────────────────────────────────────────────────────

def test_bridge_start_acquires_lock_on_resume(tmp_path):
    lp = tmp_path / ".cb_lock_sessE"
    b = bot.Bridge()
    with patch.object(bot.session, "_lock_path", return_value=lp), \
         patch.object(bot.tmux, "tmux_run", return_value=_alive_tmux()), \
         patch.object(b, "_spawn", return_value=True):
        result = b.start("sessE", chat_id=111)
    assert result is True
    assert lp.exists()


def test_bridge_start_fails_when_locked_by_other(tmp_path):
    lp = tmp_path / ".cb_lock_sessF"
    lp.write_text("other_tmux\n999")
    b = bot.Bridge()
    with patch.object(bot.session, "_lock_path", return_value=lp), \
         patch.object(bot.tmux, "tmux_run", return_value=_alive_tmux()):
        result = b.start("sessF", chat_id=111)
    assert result is False


def test_bridge_start_new_session_no_lock(tmp_path):
    """새 세션(session_id=None)은 락 획득 시도 안 함"""
    b = bot.Bridge()
    with patch.object(bot.tmux, "tmux_run", return_value=_alive_tmux()), \
         patch.object(b, "_spawn", return_value=True):
        result = b.start(None, chat_id=111)
    assert result is True
    # 락 파일 없음
    assert list(tmp_path.glob(".cb_lock_*")) == []


def test_bridge_start_releases_previous_lock(tmp_path):
    """start() 호출 시 이전 세션 락이 해제됨"""
    old_lp = tmp_path / ".cb_lock_old"
    old_lp.write_text(f"{bot.TMUX}\n111")
    new_lp = tmp_path / ".cb_lock_new"

    b = bot.Bridge()
    b.current_session_id = "old"

    def _lp(s):
        return old_lp if s == "old" else new_lp

    with patch.object(bot.session, "_lock_path", side_effect=_lp), \
         patch.object(bot.tmux, "tmux_run", return_value=_alive_tmux()), \
         patch.object(b, "_spawn", return_value=True):
        b.start("new", chat_id=111)

    assert not old_lp.exists()   # 이전 락 해제
    assert new_lp.exists()        # 새 락 획득


# ── find_sessions 락 필터 ──────────────────────────────────────────────────────

def test_find_sessions_excludes_locked_session(tmp_path, monkeypatch):
    """락 걸린 session_id는 find_sessions 목록에서 제외"""
    # PROJECTS 패치
    import time, json
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    sess_file = proj_dir / "sess_locked.jsonl"
    old_ts = time.time() - 120  # 2분 전 → 최근 60초 필터 통과
    sess_file.write_text(
        json.dumps({"type": "user", "timestamp": "2020-01-01T00:00:00Z",
                    "message": {"content": "안녕하세요 테스트 메시지입니다"}}) + "\n"
    )
    import os
    os.utime(sess_file, (old_ts, old_ts))

    monkeypatch.setattr(bot.config, "PROJECTS", tmp_path)

    lock_lp = tmp_path / ".cb_lock_sess_locked"
    lock_lp.write_text("other_tmux\n999")

    with patch.object(bot.session, "_lock_path", side_effect=lambda s: tmp_path / f".cb_lock_{s}"), \
         patch.object(bot.tmux, "tmux_run", return_value=_alive_tmux()):
        sessions = bot.find_sessions()

    ids = [s["id"] for s in sessions]
    assert "sess_locked" not in ids
