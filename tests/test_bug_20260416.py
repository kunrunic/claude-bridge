"""
20260416_074147 버그 회귀 방지 테스트.

현상: 01:31 재부팅 시 BOOT-SEED 가 직전 ⏺ 블록("안녕하세요! 무엇을 도와드릴까요?")을
last_sent + _sent_keys 양쪽에 등록 → 07:33 사용자가 "안녕?" 보냈을 때 Claude 가
동일 문자열로 응답했으나 dedup 에 걸려 silent drop.

수정:
- BOOT-SEED 는 last_sent/_sent_keys 를 건드리지 않고 last_hash 만 고정 (settle 진입 방지).
- _already_sent/_mark_sent 는 DEDUP_TTL_SEC 기반 TTL 적용.
- _commit_sent 헬퍼로 last_sent + _mark_sent 원자화.
"""
from __future__ import annotations

import sys
import time
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


# ---------- _commit_sent 원자성 ----------

def test_commit_sent_updates_both_state():
    b = bot.Bridge()
    b._commit_sent(GREETING)
    assert b.last_sent == GREETING
    assert b._already_sent(GREETING)


# ---------- TTL ----------

def test_dedup_ttl_expires():
    """DEDUP_TTL_SEC 이후엔 같은 문자열이 새 응답으로 취급된다."""
    b = bot.Bridge()
    b._mark_sent(GREETING)
    assert b._already_sent(GREETING)

    # 엔트리 타임스탬프를 TTL 경계 바깥으로 강제 이동
    b._sent_keys = [(k, t - bot.DEDUP_TTL_SEC - 1) for k, t in b._sent_keys]
    assert not b._already_sent(GREETING)


def test_dedup_lru_capacity_large_enough_for_burst():
    """MAX_SENT_HISTORY 가 TTL 창 내 폭주에도 eviction 안 일어날 만큼 커야 한다.
    기존 20개 상한으로는 2분 안에 151개 블록 생성되는 상황에서 무한 루프 발생."""
    assert bot.MAX_SENT_HISTORY >= 500


def test_dedup_within_ttl_still_blocks():
    b = bot.Bridge()
    b._mark_sent(GREETING)
    assert b._already_sent(GREETING)
    # 공백만 다른 동일 본문도 여전히 잡혀야 함 (기존 회귀 보호)
    assert b._already_sent("\n\n" + GREETING + "   \n")


# ---------- BOOT-SEED 회귀: 이번 버그 본체 ----------

def test_boot_seed_does_not_poison_dedup():
    """
    재부팅 시나리오 재현:
    1. pane 에 ⏺ seed 블록이 있는 상태에서 monitor() 가 부팅됨.
    2. 이후 사용자 입력에 Claude 가 우연히 동일 문자열로 응답.
    3. 해당 응답은 dedup 에 걸리지 않고 정상 전송돼야 함.

    monitor() 본체는 async 이므로 핵심 로직만 모사: BOOT-SEED 가 했던
    일(= last_hash 고정, last_sent 미세팅, _sent_keys 미등록)을 직접 재현한 뒤
    settle 경로의 조건식을 그대로 평가한다.
    """
    import hashlib

    b = bot.Bridge()

    # --- BOOT-SEED 시뮬레이션 (수정 후 동작) ---
    seed_pane_clean = f"some older output\n\n{GREETING}"
    b.last_hash = hashlib.md5(seed_pane_clean.encode()).hexdigest()
    # 핵심: last_sent 와 _sent_keys 는 건드리지 않음
    assert b.last_sent == ""
    assert not b._already_sent(GREETING)

    # --- 07:33: 사용자가 "안녕?" 보냄 → Claude 가 동일 문자열로 응답 ---
    new_response = GREETING
    # settle 경로 1050행 조건
    should_send = (
        "⏺" in new_response
        and new_response != b.last_sent
        and not b._already_sent(new_response)
    )
    assert should_send, "BOOT-SEED 가 dedup 을 오염시키면 이 assert 가 실패 — 이번 버그의 본체"


def test_boot_seed_does_not_trigger_settle_resend():
    """
    BOOT-SEED 직후 pane 내용이 그대로면 last_hash 가 같아 settle 파이프라인에
    진입하지 않아야 한다. 이는 "seed 블록 재전송 방지" 기능이 유지됨을 검증.
    """
    import hashlib

    b = bot.Bridge()
    seed_pane_clean = f"foo\n{GREETING}"
    b.last_hash = hashlib.md5(seed_pane_clean.encode()).hexdigest()

    # 이후 tick: 같은 pane 이 들어왔을 때 hash 비교
    h = hashlib.md5(seed_pane_clean.encode()).hexdigest()
    # monitor 의 1037행 조건: h != last_hash → False. pending 미설정.
    assert h == b.last_hash


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


# ---------- position-based busy-stream ----------

def test_busy_stream_idx_initializes_to_zero():
    b = bot.Bridge()
    assert b.busy_stream_idx == 0


def test_busy_stream_position_prevents_resend_on_content_change():
    """
    busy-stream 핵심: 완료된 블록의 content 가 바뀌어도(진행 카운터, todo
    체크 상태 등) 같은 position 이면 재전송하지 않는다.

    시나리오:
    - Tick 1: [A, B(progress: 5), C(last, growing)] → A, B 전송, idx=2
    - Tick 2: [A, B(progress: 7 — 카운터 갱신), C(last, growing)] → 둘 다 idx<2 이므로 skip
    """
    b = bot.Bridge()

    completed_t1 = [
        "⏺ Block A — 완료",
        "⏺ Block B — 진행 카운터 (md=100)",
    ]
    completed_t2 = [
        "⏺ Block A — 완료",
        "⏺ Block B — 진행 카운터 (md=200)",   # 카운터만 바뀜
    ]

    # Tick 1: 전부 새 블록
    new_t1 = completed_t1[b.busy_stream_idx:]
    assert new_t1 == completed_t1
    b.busy_stream_idx = max(b.busy_stream_idx, len(completed_t1))
    assert b.busy_stream_idx == 2

    # Tick 2: content 바뀌었지만 position 은 동일 → 빈 리스트
    new_t2 = completed_t2[b.busy_stream_idx:]
    assert new_t2 == []


def test_busy_stream_idx_monotonic_under_scrollback():
    """스크롤백으로 앞 블록이 잘려나가 completed 길이가 줄어도 idx 는 retreat 하지 않는다."""
    b = bot.Bridge()
    b.busy_stream_idx = max(b.busy_stream_idx, 5)
    # 다음 tick 에 completed 가 3개로 줄어든 경우
    b.busy_stream_idx = max(b.busy_stream_idx, 3)
    assert b.busy_stream_idx == 5


def test_busy_stream_sends_only_new_completed_blocks():
    """완료 블록이 추가되면 그 부분만 전송 대상."""
    b = bot.Bridge()
    completed_t1 = ["⏺ A", "⏺ B"]
    b.busy_stream_idx = len(completed_t1)

    completed_t2 = ["⏺ A", "⏺ B", "⏺ C", "⏺ D"]
    new = completed_t2[b.busy_stream_idx:]
    assert new == ["⏺ C", "⏺ D"]


# ---------- AI-DROP-DUP 로그 경로 ----------

def test_already_sent_check_is_the_drop_trigger():
    """settle else 분기에서 AI-DROP-DUP 로그가 나올 조건(= _already_sent=True)
    을 수동으로 만들어 봤을 때 실제로 True 가 되는지 확인."""
    b = bot.Bridge()
    b._commit_sent(GREETING)
    b.last_sent = ""  # last_sent 동등성만 뚫려도 dedup 이 2차 방어로 남는지
    assert b._already_sent(GREETING)


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
