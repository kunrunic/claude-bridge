"""
20260417_172211 회귀 방지 — tmux `-t` 의 prefix-match 로 sibling 세션 오인 조작.

문제: CB1 에서 `/end` 시 `kill-session -t claude_bridge` 가 tmux 의 prefix 매칭
규칙에 따라 `claude_bridge2` (CB2) 세션까지 kill. 추가로 `has-session` 체크도
prefix 매치로 "세션 있음" 판정을 내려 CB1 monitor 가 CB2 의 pane 을 읽는 사고.

해결: `bridge/tmux.py` 에 `_t(name)` 헬퍼 도입 — 모든 tmux `-t` 인자에 `=` 접두어
(exact-match 강제) 를 붙여 prefix 매치를 차단. `Bridge.stop()` 은 `has-session`
선행 체크로 내 세션이 없으면 `/exit`+kill 을 생략.
"""
from __future__ import annotations

import sys
import types
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock


# ── stub helpers (other test files 와 동일 패턴) ──────────────────────────────

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


def _tmux(rc: int) -> MagicMock:
    r = MagicMock()
    r.returncode = rc
    return r


# ── _t() 헬퍼 ────────────────────────────────────────────────────────────────

def test_t_helper_prefixes_equals_and_colon_for_explicit_name():
    """exact-match('=' 접두) + target-pane 호환('콜론' 접미)."""
    from bridge import tmux
    assert tmux._t("claude_bridge") == "=claude_bridge:"


def test_t_helper_defaults_to_config_tmux():
    from bridge import tmux, config
    assert tmux._t() == f"={config.TMUX}:"


def test_t_helper_rejects_prefix_match_sibling():
    """`=` 접두어가 실제로 prefix-match 를 막는지 형식 수준에서 확인."""
    from bridge import tmux
    # sibling 이름 (claude_bridge2) 이 주어져도 literal 그대로 반환되어야 한다.
    # tmux 는 '=<literal>:' 을 exact 로만 해석하므로 prefix 매치 불가.
    assert tmux._t("claude_bridge") != "=claude_bridge2:"
    assert tmux._t("claude_bridge").startswith("=")
    # 콜론 접미가 붙어야 send-keys/capture-pane (target-pane) 에서도 동작
    assert tmux._t("claude_bridge").endswith(":")


# ── Bridge.stop() kill-session 인자 검증 ─────────────────────────────────────

def test_stop_kill_session_uses_exact_match(tmp_path):
    """stop() 이 tmux kill-session 을 '=<TMUX>' 인자로 호출한다."""
    b = bot.Bridge()
    b.current_session_id = None
    calls = []

    def fake_tmux(args):
        calls.append(list(args))
        # has-session → 0 (내 세션 존재), kill-session → 0, 그 외 0
        return _tmux(0)

    async def run():
        with patch.object(bot.tmux, "tmux_run", side_effect=fake_tmux), \
             patch.object(bot.tmux, "send_input"), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            await b.stop()

    asyncio.run(run())

    kill_calls = [c for c in calls if c and c[0] == "kill-session"]
    assert kill_calls, "stop() 이 kill-session 을 호출해야 한다"
    for c in kill_calls:
        # -t 다음 인자가 '=' 로 시작해야 함
        i = c.index("-t")
        assert c[i + 1].startswith("="), f"kill-session 인자가 exact-match 가 아님: {c}"


def test_stop_skips_kill_when_my_session_absent(tmp_path):
    """내 세션이 없는 경우 (sibling 만 존재) /exit + kill 을 생략한다."""
    b = bot.Bridge()
    b.current_session_id = None
    calls = []
    send_input_calls = []

    def fake_tmux(args):
        calls.append(list(args))
        # has-session → 1 (내 세션 없음). exact-match 덕분에 sibling 이 있어도 0 안 뜸.
        if args and args[0] == "has-session":
            return _tmux(1)
        return _tmux(0)

    def fake_send_input(text):
        send_input_calls.append(text)

    async def run():
        with patch.object(bot.tmux, "tmux_run", side_effect=fake_tmux), \
             patch.object(bot.tmux, "send_input", side_effect=fake_send_input), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            await b.stop()

    asyncio.run(run())

    # kill-session 은 호출되지 않아야 한다 (sibling 오인 방지 핵심).
    kill_calls = [c for c in calls if c and c[0] == "kill-session"]
    assert not kill_calls, f"내 세션 없을 때 kill-session 이 호출됨: {kill_calls}"
    # /exit 타이핑도 생략되어야 한다 (sibling Claude TUI 에 타이핑 방지).
    assert not send_input_calls, f"send_input 이 호출됨: {send_input_calls}"


def test_start_kill_session_uses_exact_match():
    """Bridge.start() 의 선제 kill 도 '=' 접두어를 쓴다."""
    b = bot.Bridge()
    calls = []

    def fake_tmux(args):
        calls.append(list(args))
        return _tmux(0)

    with patch.object(bot.tmux, "tmux_run", side_effect=fake_tmux), \
         patch.object(b, "_spawn", return_value=True):
        b.start(None, chat_id=111)

    kill_calls = [c for c in calls if c and c[0] == "kill-session"]
    assert kill_calls, "start() 가 kill-session 을 호출해야 한다"
    for c in kill_calls:
        i = c.index("-t")
        assert c[i + 1].startswith("="), f"kill-session 인자가 exact-match 가 아님: {c}"


# ── cmd_start / cmd_esc / cmd_model / on_message has-session 인자 검증 ───────

def _has_session_call_targets(calls: list[list[str]]) -> list[str]:
    """calls 중 has-session 항목의 -t 다음 인자만 뽑는다."""
    out = []
    for c in calls:
        if c and c[0] == "has-session" and "-t" in c:
            i = c.index("-t")
            out.append(c[i + 1])
    return out


def test_cmd_start_has_session_uses_exact_match():
    """receiver.cmd_start 의 has-session 체크가 '=' 접두어를 쓴다."""
    calls = []

    def fake_tmux(args):
        calls.append(list(args))
        # 세션 있다고 응답 → 재연결 버튼 경로로 진입
        return _tmux(0)

    update = MagicMock()
    update.effective_chat.id = 111
    update.message.reply_text = AsyncMock()
    ctx = MagicMock()

    async def run():
        with patch.object(bot.config, "is_allowed", return_value=True), \
             patch.object(bot.tmux, "tmux_run", side_effect=fake_tmux):
            await bot.receiver.cmd_start(update, ctx)

    asyncio.run(run())

    targets = _has_session_call_targets(calls)
    assert targets, "cmd_start 가 has-session 을 호출해야 한다"
    for t in targets:
        assert t.startswith("="), f"has-session 인자가 exact-match 가 아님: {t}"


def test_cmd_esc_has_session_uses_exact_match():
    calls = []

    def fake_tmux(args):
        calls.append(list(args))
        return _tmux(0)

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.set_reaction = AsyncMock()
    ctx = MagicMock()

    async def run():
        with patch.object(bot.config, "is_allowed", return_value=True), \
             patch.object(bot.tmux, "tmux_run", side_effect=fake_tmux), \
             patch.object(bot.tmux, "send_key"):
            await bot.receiver.cmd_esc(update, ctx)

    asyncio.run(run())

    targets = _has_session_call_targets(calls)
    assert targets, "cmd_esc 가 has-session 을 호출해야 한다"
    for t in targets:
        assert t.startswith("="), f"has-session 인자가 exact-match 가 아님: {t}"


def test_on_message_has_session_uses_exact_match():
    """on_message 의 session_alive 체크가 '=' 접두어를 쓴다."""
    calls = []

    def fake_tmux(args):
        calls.append(list(args))
        return _tmux(1)  # 세션 없음 → reply_text 경로

    update = MagicMock()
    update.message.caption = None
    update.message.text = "hello"
    update.message.photo = None
    update.message.reply_text = AsyncMock()
    ctx = MagicMock()

    # bridge.running 이 True 여야 has-session 체크가 돌아간다
    bot.bridge.running = True

    async def run():
        with patch.object(bot.config, "is_allowed", return_value=True), \
             patch.object(bot.tmux, "tmux_run", side_effect=fake_tmux):
            await bot.receiver.on_message(update, ctx)

    try:
        asyncio.run(run())
    finally:
        bot.bridge.running = False

    targets = _has_session_call_targets(calls)
    assert targets, "on_message 가 has-session 을 호출해야 한다"
    for t in targets:
        assert t.startswith("="), f"has-session 인자가 exact-match 가 아님: {t}"


# ── lint-style: bridge/ 전역에서 '-t config.TMUX' 패턴 부재 확인 ─────────────

def test_no_raw_config_tmux_in_minus_t_arguments():
    """회귀 방지: `-t config.TMUX` 가 bridge/ 소스에 남아있으면 안 된다."""
    import re
    root = Path(__file__).resolve().parent.parent / "bridge"
    pattern = re.compile(r'"-t",\s*config\.TMUX')
    hits = []
    for p in root.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{p.name}:{lineno}: {line.strip()}")
    assert not hits, "raw `-t config.TMUX` 가 남아있음:\n" + "\n".join(hits)


if __name__ == "__main__":
    import traceback
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn() if fn.__code__.co_argcount == 0 else fn(Path("/tmp"))
                print(f"PASS {name}")
            except AssertionError:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if failures else 0)
