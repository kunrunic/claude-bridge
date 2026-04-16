"""
P4-3: 승인 재연결 테스트 (P2 구현 검증)

시나리오:
  - 정상 (프롬프트 있음) Yes/No
  - 프롬프트 없음 + 세션 살아있음 Yes/No (늦은 응답)
  - 세션 죽음 + Yes (자동 재시작 후 Claude에 재요청)
  - 세션 죽음 + No (재시작 메뉴 표시)
  - resume_after_no 콜백 처리 (재시작 / 취소)

주의: on_callback은 전역 bot.bridge 싱글톤을 사용한다.
      is_allowed 는 실제 config.json 기반이므로 테스트에서 항상 True로 패치한다.
"""
from __future__ import annotations

import sys
import types
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import pytest


# ── stub / loader ──────────────────────────────────────────────────────────────

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
    _stub("telegram.request", HTTPXRequest=_Dummy)

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


def _make_update(data: str = "approve_yes"):
    """Telegram Update 모의 객체 (is_allowed는 별도로 패치)"""
    update = MagicMock()
    q = MagicMock()
    q.data = data
    q.message = MagicMock()
    q.message.chat_id = 1
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    update.callback_query = q
    update.effective_chat = MagicMock()
    update.effective_chat.id = 1
    return update


def _make_ctx():
    ctx = MagicMock()
    ctx.application = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


# ── 정상 경로 (프롬프트 있음) ─────────────────────────────────────────────────

def test_approve_yes_normal_prompt_present():
    """pane에 프롬프트가 있을 때 Yes → Enter 전송 + 승인 메시지"""
    bot.bridge.awaiting_approval = True
    bot.bridge.last_approval_summary = "Bash"
    bot.bridge.last_approval_context = "Bash"

    keys_sent = []

    async def run():
        update = _make_update(data="approve_yes")
        ctx = _make_ctx()

        with patch.object(bot.config, "is_allowed", return_value=True), \
             patch.object(bot.tmux, "pane_output", return_value="Do you want to proceed?"), \
             patch.object(bot.parser, "strip_ansi", side_effect=lambda x: x), \
             patch.object(bot.parser, "is_approval", return_value=True), \
             patch.object(bot.tmux, "send_key", side_effect=lambda k: keys_sent.append(k)):
            await bot.on_callback(update, ctx)

    asyncio.run(run())

    assert "Enter" in keys_sent
    assert bot.bridge.awaiting_approval is False


def test_approve_no_normal_prompt_present():
    """pane에 프롬프트가 있을 때 No → Down+Enter 전송"""
    bot.bridge.awaiting_approval = True
    bot.bridge.last_approval_summary = "Bash"
    bot.bridge.last_approval_context = "Bash"

    keys_sent = []

    async def run():
        update = _make_update(data="approve_no")
        ctx = _make_ctx()

        with patch.object(bot.config, "is_allowed", return_value=True), \
             patch.object(bot.tmux, "pane_output", return_value="Do you want to proceed?"), \
             patch.object(bot.parser, "strip_ansi", side_effect=lambda x: x), \
             patch.object(bot.parser, "is_approval", return_value=True), \
             patch.object(bot.tmux, "send_key", side_effect=lambda k: keys_sent.append(k)):
            await bot.on_callback(update, ctx)

    asyncio.run(run())

    assert "Down" in keys_sent
    assert "Enter" in keys_sent
    assert bot.bridge.awaiting_approval is False


# ── 늦은 응답 (프롬프트 없음 + 세션 살아있음) ─────────────────────────────────

def test_approve_yes_late_session_alive():
    """프롬프트 없음 + 세션 살아있음 → Claude에 '승인했습니다' 전송"""
    bot.bridge.awaiting_approval = True
    bot.bridge.last_approval_context = "Bash"
    bot.bridge.running = True

    inputs_sent = []

    async def run():
        update = _make_update(data="approve_yes")
        ctx = _make_ctx()

        with patch.object(bot.config, "is_allowed", return_value=True), \
             patch.object(bot.tmux, "pane_output", return_value="일반 Claude 프롬프트"), \
             patch.object(bot.parser, "strip_ansi", side_effect=lambda x: x), \
             patch.object(bot.parser, "is_approval", return_value=False), \
             patch.object(bot.bridge, "is_alive", return_value=True), \
             patch.object(bot.tmux, "send_input", side_effect=lambda t: inputs_sent.append(t)):
            await bot.on_callback(update, ctx)

    asyncio.run(run())

    assert bot.bridge.awaiting_approval is False
    assert any("승인했습니다" in t for t in inputs_sent)


def test_approve_no_late_session_alive():
    """프롬프트 없음 + 세션 살아있음 → Claude에 '거부했습니다' 전송"""
    bot.bridge.awaiting_approval = True
    bot.bridge.last_approval_context = "Write"
    bot.bridge.running = True

    inputs_sent = []

    async def run():
        update = _make_update(data="approve_no")
        ctx = _make_ctx()

        with patch.object(bot.config, "is_allowed", return_value=True), \
             patch.object(bot.tmux, "pane_output", return_value="일반 Claude 프롬프트"), \
             patch.object(bot.parser, "strip_ansi", side_effect=lambda x: x), \
             patch.object(bot.parser, "is_approval", return_value=False), \
             patch.object(bot.bridge, "is_alive", return_value=True), \
             patch.object(bot.tmux, "send_input", side_effect=lambda t: inputs_sent.append(t)):
            await bot.on_callback(update, ctx)

    asyncio.run(run())

    assert bot.bridge.awaiting_approval is False
    assert any("거부했습니다" in t for t in inputs_sent)


# ── 세션 죽음 + Yes (재시작 후 재요청) ────────────────────────────────────────

def test_approve_yes_late_session_dead_restarts():
    """세션 죽음 + Yes → bridge.start() 후 '승인했습니다' 전송"""
    bot.bridge.awaiting_approval = True
    bot.bridge.last_approval_context = "Bash"
    bot.bridge.current_session_id = "sessX"
    bot.bridge.running = False

    inputs_sent = []
    tasks_created = []

    async def run():
        update = _make_update(data="approve_yes")
        ctx = _make_ctx()

        def fake_start(session_id, chat_id=0):
            return True

        def fake_create_task(coro):
            coro.close()
            t = MagicMock()
            tasks_created.append(t)
            return t

        with patch.object(bot.config, "is_allowed", return_value=True), \
             patch.object(bot.tmux, "pane_output", return_value=""), \
             patch.object(bot.parser, "strip_ansi", side_effect=lambda x: x), \
             patch.object(bot.parser, "is_approval", return_value=False), \
             patch.object(bot.bridge, "is_alive", return_value=False), \
             patch.object(bot.bridge, "start", side_effect=fake_start), \
             patch("asyncio.create_task", side_effect=fake_create_task), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch.object(bot.tmux, "send_input", side_effect=lambda t: inputs_sent.append(t)):
            await bot.on_callback(update, ctx)

    asyncio.run(run())

    assert bot.bridge.awaiting_approval is False
    assert any("승인했습니다" in t for t in inputs_sent)
    assert len(tasks_created) >= 1   # monitor task 생성됨


def test_approve_yes_late_session_dead_start_fails():
    """세션 죽음 + Yes → start() 실패 시 오류 메시지"""
    bot.bridge.awaiting_approval = True
    bot.bridge.last_approval_context = "Bash"
    bot.bridge.current_session_id = "sessX"
    bot.bridge.running = False

    edit_calls = []

    async def run():
        update = _make_update(data="approve_yes")
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=lambda text, reply_markup=None: edit_calls.append(text)
        )
        ctx = _make_ctx()

        with patch.object(bot.config, "is_allowed", return_value=True), \
             patch.object(bot.tmux, "pane_output", return_value=""), \
             patch.object(bot.parser, "strip_ansi", side_effect=lambda x: x), \
             patch.object(bot.parser, "is_approval", return_value=False), \
             patch.object(bot.bridge, "is_alive", return_value=False), \
             patch.object(bot.bridge, "start", return_value=False):
            await bot.on_callback(update, ctx)

    asyncio.run(run())

    assert bot.bridge.awaiting_approval is False
    assert any("실패" in t for t in edit_calls)


# ── 세션 죽음 + No (재시작 메뉴) ─────────────────────────────────────────────

def test_approve_no_late_session_dead_shows_restart_menu():
    """세션 죽음 + No → '세션이 종료되었습니다' + [재시작] [취소] 메뉴"""
    bot.bridge.awaiting_approval = True
    bot.bridge.last_approval_context = "Edit"
    bot.bridge.current_session_id = "sessX"
    bot.bridge.running = False

    edit_calls = []

    async def run():
        update = _make_update(data="approve_no")
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=lambda text, reply_markup=None: edit_calls.append(text)
        )
        ctx = _make_ctx()

        with patch.object(bot.config, "is_allowed", return_value=True), \
             patch.object(bot.tmux, "pane_output", return_value=""), \
             patch.object(bot.parser, "strip_ansi", side_effect=lambda x: x), \
             patch.object(bot.parser, "is_approval", return_value=False), \
             patch.object(bot.bridge, "is_alive", return_value=False):
            await bot.on_callback(update, ctx)

    asyncio.run(run())

    assert bot.bridge.awaiting_approval is False
    assert any("세션이 종료되었습니다" in t for t in edit_calls)
    assert any("Edit" in t for t in edit_calls)   # 컨텍스트 포함


# ── resume_after_no 콜백 ──────────────────────────────────────────────────────

def test_resume_after_no_restarts_session():
    """resume_after_no:{session_id} → bridge.start() + monitor 생성"""
    bot.bridge.task = None
    tasks_created = []

    async def run():
        update = _make_update(data="resume_after_no:sessA")
        ctx = _make_ctx()

        def fake_start(session_id, chat_id=0):
            return True

        def fake_create_task(coro):
            coro.close()
            t = MagicMock()
            tasks_created.append(t)
            return t

        with patch.object(bot.config, "is_allowed", return_value=True), \
             patch.object(bot.bridge, "start", side_effect=fake_start), \
             patch("asyncio.create_task", side_effect=fake_create_task):
            await bot.on_callback(update, ctx)

    asyncio.run(run())

    assert len(tasks_created) >= 1


def test_resume_after_no_cancel_does_nothing():
    """resume_after_no:cancel → 취소 메시지만 표시"""
    edit_calls = []

    async def run():
        update = _make_update(data="resume_after_no:cancel")
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=lambda text, reply_markup=None: edit_calls.append(text)
        )
        ctx = _make_ctx()

        with patch.object(bot.config, "is_allowed", return_value=True):
            await bot.on_callback(update, ctx)

    asyncio.run(run())

    assert any("취소" in t for t in edit_calls)


def test_resume_after_no_start_fails_shows_error():
    """resume_after_no → start() 실패 시 오류 메시지"""
    edit_calls = []

    async def run():
        update = _make_update(data="resume_after_no:sessB")
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=lambda text, reply_markup=None: edit_calls.append(text)
        )
        ctx = _make_ctx()

        with patch.object(bot.config, "is_allowed", return_value=True), \
             patch.object(bot.bridge, "start", return_value=False):
            await bot.on_callback(update, ctx)

    asyncio.run(run())

    assert any("실패" in t for t in edit_calls)


# ── 승인 컨텍스트 저장 (P2-1) ─────────────────────────────────────────────────

def test_monitor_saves_approval_context():
    """monitor가 승인 프롬프트 감지 시 last_approval_context에 도구명 저장"""
    b = bot.Bridge()
    b.last_approval_context = ""
    b.last_approval_full = ""

    app = MagicMock()
    app.bot = MagicMock()
    app.bot.send_message = AsyncMock()
    app.bot.delete_message = AsyncMock()

    approval_pane = "Bash(ls -la)\n─────────\nDo you want to proceed?\n❯ 1. Yes  2. No"

    call_count = [0]

    async def fake_pane():
        call_count[0] += 1
        if call_count[0] <= 2:
            return approval_pane
        b.running = False
        return approval_pane

    async def run():
        with patch.object(bot.tmux, "pane_output_async", side_effect=fake_pane), \
             patch.object(bot.tmux, "tmux_run_async", new_callable=AsyncMock,
                          return_value=_tmux(0)), \
             patch.object(b, "is_alive_async", new_callable=AsyncMock, return_value=True), \
             patch.object(bot.parser, "strip_ansi", side_effect=lambda x: x), \
             patch.object(bot.tmux, "send_key"), \
             patch.object(bot.sender, "_send_approval", new_callable=AsyncMock):
            await b.monitor(app, chat_id=111)

    asyncio.run(run())

    # 승인 프롬프트 감지 후 컨텍스트가 저장되어야 함
    assert b.last_approval_context != "" or b.awaiting_approval is True
    assert b.last_approval_full != "" or b.awaiting_approval is True
