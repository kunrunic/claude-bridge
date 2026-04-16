"""
20260416_074147 회귀 방지 테스트 (재설계본).

구 설계: content-hash dedup (_sent_keys, TTL, LRU) 로 BOOT-SEED 블록이
다시 전송되지 않게 막았지만, 같은 문자열이 새 turn 에 우연히 나오면
silent drop 되는 버그가 있었다.

신 설계: position-based `StreamQueue` — pane 의 ⏺ 블록 인덱스만 추적.
- "전송 = 소비" → content 같아도 새 position 이면 새 블록.
- BOOT-SEED 시 queue.seed(N) 으로 기존 블록 N 개를 '소비됨' 으로 표시.
- 새 user turn 에서 queue.reset() → idx=0.

이 파일은 StreamQueue 동작과 BOOT-SEED 재전송 방지 회귀, 그리고
_is_block_active (busy-stream 이 의존하는 활성 블록 감지) 를 지킨다.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path


def _stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _load_bot():
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
        cfg.write_text('{"token":"x","claude_path":"/bin/true","allowed_ids":[1],"tmux_session":"t"}')

    import importlib
    if "bot" in sys.modules:
        del sys.modules["bot"]
    return importlib.import_module("bot")


bot = _load_bot()

GREETING = "⏺ 안녕하세요! 무엇을 도와드릴까요?"


# ---------- StreamQueue 기본 동작 ----------

def test_queue_starts_empty():
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    assert q.idx == 0
    assert q.take_new(["⏺ A", "⏺ B"]) == ["⏺ A", "⏺ B"]


def test_queue_take_new_is_non_destructive():
    """take_new 는 idx 를 움직이지 않는다 — 호출자가 advance 로 명시적으로 전진."""
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    completed = ["⏺ A", "⏺ B"]
    assert q.take_new(completed) == completed
    assert q.take_new(completed) == completed  # 여전히 동일
    assert q.idx == 0


def test_queue_advance_marks_consumed():
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    completed = ["⏺ A", "⏺ B"]
    q.advance(len(completed))
    assert q.take_new(completed) == []


def test_queue_advance_is_monotonic():
    """스크롤백으로 completed 길이가 줄어도 idx 는 retreat 금지."""
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    q.advance(5)
    q.advance(3)   # 후퇴 시도
    assert q.idx == 5


def test_queue_seed_treats_existing_as_consumed():
    """BOOT-SEED: pane 에 이미 있던 블록은 소비된 것으로 간주."""
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    q.seed(3)
    assert q.take_new(["⏺ A", "⏺ B", "⏺ C"]) == []
    # 네 번째가 새로 붙으면 그것만 new
    assert q.take_new(["⏺ A", "⏺ B", "⏺ C", "⏺ D"]) == ["⏺ D"]


def test_queue_reset_returns_to_zero():
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    q.advance(10)
    q.reset()
    assert q.idx == 0


def test_queue_position_not_content():
    """position-based: content 가 바뀌어도 같은 idx 면 소비된 것.

    busy-stream 에서 진행 카운터(todo 체크, N/total 진행도 등)만 바뀌는 블록이
    재전송되지 않음을 보장.
    """
    from bridge.stream_queue import StreamQueue
    q = StreamQueue()
    completed_t1 = ["⏺ A", "⏺ B (progress 5)"]
    new = q.take_new(completed_t1)
    q.advance(len(completed_t1))
    assert len(new) == 2

    completed_t2 = ["⏺ A", "⏺ B (progress 7)"]  # B 의 content 바뀜
    assert q.take_new(completed_t2) == []  # position 이 같으니 skip


# ---------- BOOT-SEED 재전송 방지 회귀 ----------

def test_boot_seed_does_not_poison_new_turn():
    """
    재부팅 시나리오:
    1. pane 에 ⏺ greeting 이 있는 상태에서 monitor() 가 부팅됨.
       → queue.seed(1) 으로 idx=1.
    2. 이후 사용자가 "안녕?" 보냄 → queue.reset() 으로 idx=0.
    3. Claude 가 우연히 동일 문자열로 응답 → 새 block 으로 인식, 전송됨.

    구 설계는 content-hash dedup 에 막혀 silent drop 됐음.
    """
    b = bot.Bridge()
    # BOOT-SEED 시뮬레이션
    b.queue.seed(1)
    assert b.queue.idx == 1

    # 사용자가 새 turn 시작
    b.queue.reset()
    assert b.queue.idx == 0

    # Claude 가 응답 — queue 는 새 block 으로 인식
    new_turn_blocks = [GREETING]
    assert b.queue.take_new(new_turn_blocks) == [GREETING]


def test_boot_seed_prevents_settle_resend():
    """BOOT-SEED 직후 pane 이 그대로면 settle 이 작동하지 않아야 한다."""
    import hashlib

    b = bot.Bridge()
    seed_pane = f"foo\n{GREETING}"
    b.last_hash = hashlib.md5(seed_pane.encode()).hexdigest()
    b.queue.seed(1)  # ⏺ greeting 1개 consumed

    # 다음 tick: 같은 pane
    h = hashlib.md5(seed_pane.encode()).hexdigest()
    assert h == b.last_hash   # settle 파이프라인 미진입

    # 설령 진입해도 queue 가 막아준다
    assert b.queue.take_new([GREETING]) == []


# ---------- _is_block_active: 실행 중 도구 블록 감지 ----------

def test_active_block_running():
    blk = (
        "⏺ Bash(docker exec qmd qmd query \"test\" 2>&1)\n"
        "  ⎿  Running… (1m 19s · timeout 2m)"
    )
    assert bot._is_block_active(blk)


def test_active_block_waiting():
    blk = (
        "⏺ Bash(docker exec qmd qmd vsearch \"test\")\n"
        "  ⎿  Waiting…\n"
        "     (ctrl+b ctrl+b (twice) to run in background)"
    )
    assert bot._is_block_active(blk)


def test_completed_block_not_active():
    blk = (
        "⏺ Bash(docker exec qmd qmd query \"test\")\n"
        "  ⎿  Title: VoLTE 설계\n"
        "     Score: 0.85"
    )
    assert not bot._is_block_active(blk)


def test_text_response_not_active():
    assert not bot._is_block_active(GREETING)


# ---------- Bridge 에 StreamQueue 가 붙어있는지 ----------

def test_bridge_has_stream_queue():
    from bridge.stream_queue import StreamQueue
    b = bot.Bridge()
    assert isinstance(b.queue, StreamQueue)
    assert b.queue.idx == 0


if __name__ == "__main__":
    import traceback
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if failures else 0)
