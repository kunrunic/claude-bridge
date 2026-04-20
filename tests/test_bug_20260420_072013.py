"""20260420_072013 회귀 방지 — 연속 사용자 입력 race 로 응답 블록 stranding.

사건 요약
---------
07:20:12 msg1: "신규 인경우에는? 새로운걸 추가해야되는 경우야"
07:20:13 msg2: (1.3초 뒤) HF Zone 설명 문서 forward
07:21~  Claude 의 msg1 응답 `⏺ 좋은 질문 — 지금은 신규 서비스 추가 흐름 자체가 없음…`
         블록이 **텔레그램으로 영영 전달되지 않음**. 사용자는 ESC 후 재송신해 복구.

근본 원인
---------
`parser.extract_response_blocks()` 는 응답 영역 내 "**마지막** ❯ 사용자 입력"
을 앵커로 삼아 그 **이후** 의 ⏺ 블록만 추출한다 (lines 327-336).

msg2 가 pane 에 들어가면 새 ❯ 프롬프트가 들어서면서 msg1 에 대한 응답인
`⏺ 좋은 질문` 블록이 앵커 **앞** 으로 밀려 extract 결과에서 제외된다.
이 시점에 race 창이 열린다:

  1. on_message(msg2) 가 `bridge.queue.reset()` 으로 idx/last_fp 를 0/"" 로.
  2. Claude 가 msg1 응답을 pane 에 push 완료. extract_response_blocks 는
     새 ❯ 앵커 너머만 보므로 `⏺ 좋은 질문` 은 이미 "보이지 않음".
  3. monitor 의 다음 _flush_completed 는 앵커 뒷블록만 본다 → `⏺ 좋은 질문`
     영구 stranded.

수정 방향 (Phase 1 — A+)
------------------------
1. `Bridge._queue_lock` — reset+send_input 과 _flush_completed 를 직렬화.
2. `_emergency_flush_before_input` — reset **직전** 한 번 flush 시도.
   flush 가 실패하면 reset 을 건너뛰어 idx/last_fp 를 보존 (stranded 재발 방지).
3. `BUSY_STREAM_SEC = 10 → 3` — busy 중 주기적 flush 간격 축소로 race 창 감소.

본 테스트는 위 3가지를 각각 검증한다.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path


# -- Telegram 스텁 (test_core_monitor.py 패턴) --------------------------------

def _stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _prep():
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

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    cfg = root / "config.json"
    if not cfg.exists():
        cfg.write_text(
            '{"token":"x","claude_path":"claude","allowed_ids":[1],'
            '"tmux_session":"claude_bridge_test"}'
        )


_prep()

from bridge import config as config_mod  # noqa: E402
from bridge import parser  # noqa: E402
from bridge import sender as sender_mod  # noqa: E402
from bridge import tmux as tmux_mod  # noqa: E402
from bridge.core import Bridge  # noqa: E402


class _FakeBot:
    async def send_message(self, *a, **kw):
        return None


class _FakeApp:
    bot = _FakeBot()


# -- 핵심 fixture -------------------------------------------------------------

# msg1 답변 (`⏺ 좋은 질문`) 이 완성된 상태, **msg2 가 들어오기 전** pane.
# - 마지막 ❯ 앵커는 msg1 → 그 뒤의 `⏺ 좋은 질문` 은 정상적으로 추출됨.
PANE_BEFORE_MSG2 = """\
❯ 신규 인경우에는? 새로운걸 추가해야되는 경우야

⏺ 좋은 질문 — 지금은 신규 서비스 추가 흐름 자체가 없음. 현재 설계는 기존
  서비스에 대한 조회/조작에만 초점이 맞춰져 있어, 신규 서비스 정의 자체를
  시스템에 주입하는 경로가 비어있는 상태.

  1. service catalog 를 제공자/수신자 양측에서 주입 가능하게
  2. Zone 단위로 catalog 를 분리 보관
"""


# msg2 forward 가 pane 에 들어간 직후 — **새 ❯ 앵커** 가 pane 맨 아래 등장.
# extract_response_blocks 의 last-❯ 앵커 설계 상, 이 시점부터 `⏺ 좋은 질문`
# 은 앵커보다 위에 있어 추출 대상에서 제외된다 (= stranded).
PANE_AFTER_MSG2 = """\
❯ 신규 인경우에는? 새로운걸 추가해야되는 경우야

⏺ 좋은 질문 — 지금은 신규 서비스 추가 흐름 자체가 없음. 현재 설계는 기존
  서비스에 대한 조회/조작에만 초점이 맞춰져 있어, 신규 서비스 정의 자체를
  시스템에 주입하는 경로가 비어있는 상태.

  1. service catalog 를 제공자/수신자 양측에서 주입 가능하게
  2. Zone 단위로 catalog 를 분리 보관

❯ HF Zone 문서: …(forward 본문)…
"""


# ===========================================================================
# 1. 근본 원인 증명 — extract_response_blocks 의 last-❯ 앵커 설계
# ===========================================================================

def test_root_cause_extract_blocks_before_new_prompt():
    """msg2 도착 **전** pane 에서는 `⏺ 좋은 질문` 이 정상 추출된다."""
    blocks = parser.extract_response_blocks(PANE_BEFORE_MSG2)
    assert len(blocks) == 1
    assert blocks[0].startswith("⏺ 좋은 질문")


def test_root_cause_block_stranded_after_new_prompt():
    """msg2 의 새 ❯ 가 들어오면 `⏺ 좋은 질문` 은 앵커 **앞** 으로 밀려
    extract 결과에서 제외된다. 이것이 stranding 의 근본 원인."""
    blocks = parser.extract_response_blocks(PANE_AFTER_MSG2)
    # 앵커가 새 msg2 ❯ 로 이동 → 그 뒤엔 ⏺ 블록이 없음.
    assert blocks == [], (
        "last-❯ 앵커 설계상 이전 turn 의 ⏺ 블록은 추출 불가 — "
        "emergency flush 없이 queue.reset() 하면 영구 stranded."
    )


# ===========================================================================
# 2. 수정 검증 — emergency flush 가 reset 前에 실행되면 블록이 구출된다
# ===========================================================================

def test_emergency_flush_rescues_prior_block_before_reset(monkeypatch):
    """on_message 시퀀스의 핵심: emergency flush → reset → send_input 순서.

    - flush 단계에서 PANE_BEFORE_MSG2 기준으로 `⏺ 좋은 질문` 을 전송.
    - 그 직후 reset 해도 이미 텔레그램으로 나간 뒤라 stranded 되지 않음.
    """
    sent: list[str] = []

    async def fake_send(app, chat_id, text):
        sent.append(text)

    async def fake_pane():
        return PANE_BEFORE_MSG2

    monkeypatch.setattr(sender_mod, "_send_output", fake_send)
    monkeypatch.setattr(tmux_mod, "pane_output_async", fake_pane)

    br = Bridge()
    # Bridge.running / task 를 "살아있음" 으로 위장 —
    # _emergency_flush_before_input 의 running 가드 통과용.
    br.running = True

    async def run():
        br.task = asyncio.get_running_loop().create_future()
        async with br._queue_lock:
            ok = await br._emergency_flush_before_input(_FakeApp(), chat_id=0)
            assert ok is True
            # flush 성공 이후에만 reset 호출.
            br.queue.reset()

    asyncio.run(run())

    assert len(sent) == 1
    assert sent[0].startswith("⏺ 좋은 질문"), (
        "emergency flush 가 reset 前에 실행되어 `⏺ 좋은 질문` 을 구출해야 한다."
    )
    # reset 으로 idx 가 0 복귀.
    assert br.queue.idx == 0
    assert br.queue.last_fp == ""


def test_reset_skipped_when_emergency_flush_fails(monkeypatch):
    """emergency flush 가 예외로 실패하면 reset 을 **건너뛰어** 기존
    idx/last_fp 를 보존해야 stranded 재발을 막을 수 있다."""
    async def fake_pane():
        raise RuntimeError("pane capture broken")

    monkeypatch.setattr(tmux_mod, "pane_output_async", fake_pane)

    br = Bridge()
    br.running = True

    # 기존 상태 — 이전 turn 에서 이미 소비된 블록 하나가 있다고 가정.
    br.queue.advance(3)
    br.queue.mark_sent("⏺ previous_sent_block")
    saved_idx = br.queue.idx
    saved_fp = br.queue.last_fp

    async def run():
        br.task = asyncio.get_running_loop().create_future()
        async with br._queue_lock:
            ok = await br._emergency_flush_before_input(_FakeApp(), chat_id=0)
            # receiver.py 의 정책: flush 실패 시 reset 스킵.
            if ok:
                br.queue.reset()
        return ok

    ok = asyncio.run(run())
    assert ok is False, "pane capture 실패 시 flush 는 False 반환."

    # flush 실패 → reset 안 됨 → idx/last_fp 그대로.
    assert br.queue.idx == saved_idx
    assert br.queue.last_fp == saved_fp


# ===========================================================================
# 3. 동시성 — _queue_lock 이 flush 와 reset 을 직렬화한다
# ===========================================================================

def test_queue_lock_serializes_flush_and_reset(monkeypatch):
    """monitor 의 _flush_completed 와 receiver 의 emergency flush+reset 이
    동시에 동작하려 해도 _queue_lock 으로 직렬화되어야 한다.

    race 재현: reset 과 flush 를 gather 로 동시 스타트. flush 가 먼저 들어가
    `⏺ 좋은 질문` 을 내보낸 뒤에야 reset 이 진행되거나, 반대 순서면 flush 가
    빈 결과를 받는다. 어느 쪽이든 **중간에 끼어드는 부분 상태는 없어야** 한다.
    """
    send_calls: list[str] = []
    in_send = [False]
    overlap_detected = [False]

    async def slow_send(app, chat_id, text):
        # 가짜 Telegram send 는 약간의 await 를 가진다 — race 창 연출.
        if in_send[0]:
            overlap_detected[0] = True
        in_send[0] = True
        await asyncio.sleep(0.01)
        send_calls.append(text)
        in_send[0] = False

    async def fake_pane():
        return PANE_BEFORE_MSG2

    monkeypatch.setattr(sender_mod, "_send_output", slow_send)
    monkeypatch.setattr(tmux_mod, "pane_output_async", fake_pane)

    br = Bridge()
    br.running = True

    async def reset_path():
        async with br._queue_lock:
            ok = await br._emergency_flush_before_input(_FakeApp(), 0)
            if ok:
                br.queue.reset()

    async def monitor_flush_path():
        # monitor 측 _flush_completed 도 _queue_lock 안에서 동작.
        await br._flush_completed(
            _FakeApp(), 0,
            parser.strip_ansi(PANE_BEFORE_MSG2).strip(),
            include_last=True, log_tag="monitor",
        )

    async def run():
        br.task = asyncio.get_running_loop().create_future()
        await asyncio.gather(reset_path(), monitor_flush_path())

    asyncio.run(run())

    # 본 테스트의 핵심 invariant: 두 path 의 send 가 절대 **인터리브** 되지 않는다.
    # 인터리브 되면 한 path 의 advance_past 가 다른 path 의 take_new 중간에 끼어들어
    # idx/last_fp 가 inconsistent 한 상태로 남는다. 이것이 stranding 의 직접 원인.
    # (두 path 모두 동일 pane 을 보고 있어 중복 send 는 테스트 아티팩트 — 실제로는
    #  reset 직후 tmux.send_input 이 pane 을 바꾸므로 monitor 의 다음 flush 는
    #  새 블록을 본다.)
    assert overlap_detected[0] is False, (
        "_queue_lock 이 깨져 monitor flush 와 emergency flush 가 인터리브됐다."
    )
    # 적어도 한 번은 전송되어야 한다 (stranding 방지).
    good = [t for t in send_calls if t.startswith("⏺ 좋은 질문")]
    assert len(good) >= 1, f"`⏺ 좋은 질문` 이 최소 1회는 전송되어야: calls={send_calls}"


# ===========================================================================
# 4. BUSY_STREAM_SEC — race 창 축소 튜닝
# ===========================================================================

def test_busy_stream_sec_reduced_to_3():
    """busy 중 주기적 flush 간격이 10→3 초로 축소되어 race 창이 줄었다.

    값 자체가 "합리적 작은 수" 인지 검사 (튜닝이 되돌려지면 실패).
    """
    assert config_mod.BUSY_STREAM_SEC <= 3, (
        f"BUSY_STREAM_SEC must be ≤ 3s for race window reduction "
        f"(got {config_mod.BUSY_STREAM_SEC})"
    )
    assert config_mod.BUSY_STREAM_SEC >= 1, (
        "너무 작으면 busy 중 flush 폭주 — 최소 1초."
    )
