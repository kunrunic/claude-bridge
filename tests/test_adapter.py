"""BridgeAdapter 단위 테스트 — §13.1 skeleton + §13.11 반영 동작."""
from __future__ import annotations

import asyncio

import pytest

from bridge.adapter import (
    ACTION_BUSY_CLEAR,
    ACTION_BUSY_THROTTLE,
    ACTION_DISPATCH_APPROVAL,
    ACTION_DISPATCH_APPROVAL_CANCEL,
    ACTION_DISPATCH_COMPACT_COMPLETE,
    ACTION_DISPATCH_COMPACT_START,
    ACTION_DISPATCH_RESPONSE,
    ACTION_SUPPRESS,
    AdapterConfig,
    BridgeAdapter,
    resolve_action,
)
from tools.event_classifier import AnalyzerEvent


class _FakeSender:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def send_response(self, text, kind):
        self.calls.append(("send_response", (text, kind)))

    async def send_approval(self, tool_hint, summary, box_text):
        self.calls.append(("send_approval", (tool_hint, summary, box_text)))

    async def notify_cancel(self, reason):
        self.calls.append(("notify_cancel", (reason,)))

    async def notify(self, text):
        self.calls.append(("notify", (text,)))

    async def notify_limit(self, message):
        self.calls.append(("notify_limit", (message,)))


class _FakeDump:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def event(self, source, kind, **fields):
        self.calls.append((source, kind, fields))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# -- resolve_action -----------------------------------------------------------


def test_resolve_action_block_commit_dispatch_response():
    assert resolve_action("block_commit", AdapterConfig()) == ACTION_DISPATCH_RESPONSE


def test_resolve_action_busy_suppressed_by_default():
    cfg = AdapterConfig()
    assert resolve_action("busy_enter", cfg) == ACTION_SUPPRESS
    assert resolve_action("busy_exit", cfg) == ACTION_SUPPRESS


def test_resolve_action_busy_dispatched_when_enabled():
    cfg = AdapterConfig(busy_dispatch=True)
    assert resolve_action("busy_enter", cfg) == ACTION_BUSY_THROTTLE
    assert resolve_action("busy_exit", cfg) == ACTION_BUSY_CLEAR


def test_resolve_action_boot_banner_suppressed_by_default():
    cfg = AdapterConfig()
    assert resolve_action("session_boot", cfg) == ACTION_SUPPRESS
    assert resolve_action("session_resume", cfg) == ACTION_SUPPRESS


def test_resolve_action_approval_cancel_notify_default():
    cfg = AdapterConfig()
    assert resolve_action("approval_cancel", cfg) == ACTION_DISPATCH_APPROVAL_CANCEL


def test_resolve_action_approval_cancel_suppressed_when_disabled():
    cfg = AdapterConfig(approval_cancel_notify=False)
    assert resolve_action("approval_cancel", cfg) == ACTION_SUPPRESS


def test_resolve_action_user_echo_always_suppressed():
    assert resolve_action("user_echo", AdapterConfig()) == ACTION_SUPPRESS


def test_resolve_action_compact_start_and_complete():
    cfg = AdapterConfig()
    assert resolve_action("compact_start", cfg) == ACTION_DISPATCH_COMPACT_START
    assert resolve_action("compact_complete", cfg) == ACTION_DISPATCH_COMPACT_COMPLETE


def test_resolve_action_unknown_event_suppressed():
    assert resolve_action("unknown_xyz", AdapterConfig()) == ACTION_SUPPRESS


# -- BridgeAdapter --------------------------------------------------------------


def test_adapter_shadow_mode_records_action_without_dispatch():
    dump = _FakeDump()
    sender = _FakeSender()
    ad = BridgeAdapter(sender=sender, dump_writer=dump, config=AdapterConfig(dispatch_enabled=False))
    ev = AnalyzerEvent(offset=100, t="block_commit", region="content",
                       payload={"text": "hello", "kind": "response"})
    action = _run(ad.process_event(ev))
    assert action == ACTION_DISPATCH_RESPONSE
    assert sender.calls == []  # dispatch 꺼져있음
    assert any(d[1] == "process_event" for d in dump.calls)


def test_adapter_dispatch_enabled_invokes_sender():
    sender = _FakeSender()
    ad = BridgeAdapter(sender=sender, config=AdapterConfig(dispatch_enabled=True))
    ev = AnalyzerEvent(offset=100, t="block_commit", region="content",
                       payload={"text": "hello", "kind": "response"})
    _run(ad.process_event(ev))
    assert sender.calls == [("send_response", ("hello", "response"))]


def test_adapter_approval_cancel_flush_triggers_notify_cancel():
    """§13.1.b — flush cancel 은 즉시 통보 (case-04 회귀 방지)."""
    sender = _FakeSender()
    ad = BridgeAdapter(sender=sender, config=AdapterConfig(dispatch_enabled=True))
    ev = AnalyzerEvent(offset=100, t="approval_cancel", region="modal_overlay",
                       payload={"method": "flush"})
    _run(ad.process_event(ev))
    assert any(c[0] == "notify_cancel" for c in sender.calls)


def test_adapter_busy_throttle_dispatches_only_once_per_window():
    cfg = AdapterConfig(dispatch_enabled=True, busy_dispatch=True, busy_throttle_sec=5.0)
    now = [0.0]
    sender = _FakeSender()
    ad = BridgeAdapter(sender=sender, config=cfg, clock=lambda: now[0])
    ev = AnalyzerEvent(offset=100, t="busy_enter", region="content",
                       payload={"label": "Thinking"})
    a1 = _run(ad.process_event(ev))
    now[0] = 2.0
    a2 = _run(ad.process_event(ev))
    now[0] = 6.0
    a3 = _run(ad.process_event(ev))
    assert a1 == ACTION_BUSY_THROTTLE
    assert a2 == ACTION_SUPPRESS  # 아직 window
    assert a3 == ACTION_BUSY_THROTTLE
    notify_calls = [c for c in sender.calls if c[0] == "notify"]
    assert len(notify_calls) == 2


def test_adapter_register_send_input_stores_entry():
    ad = BridgeAdapter(config=AdapterConfig())
    ad.register_send_input(500, "hello")
    assert len(ad.state.send_inputs) == 1
    offset, text, _ts = ad.state.send_inputs[0]
    assert (offset, text) == (500, "hello")


def test_adapter_register_send_input_dumps():
    dump = _FakeDump()
    ad = BridgeAdapter(config=AdapterConfig(), dump_writer=dump)
    ad.register_send_input(500, "hello")
    assert any(c[1] == "register_send_input" for c in dump.calls)


def test_adapter_compact_dispatches_notify_pair():
    sender = _FakeSender()
    ad = BridgeAdapter(sender=sender, config=AdapterConfig(dispatch_enabled=True))
    _run(ad.process_event(AnalyzerEvent(
        offset=10, t="compact_start", region="content", payload={"trigger": "/compact"}
    )))
    _run(ad.process_event(AnalyzerEvent(
        offset=50, t="compact_complete", region="content", payload={"trigger": "Crunched"}
    )))
    notify_calls = [c for c in sender.calls if c[0] == "notify"]
    assert len(notify_calls) == 2
    # 순서 확인: 압축 중 → 완료
    assert "압축 중" in notify_calls[0][1][0]
    assert "완료" in notify_calls[1][1][0]


def test_adapter_flush_resets_state():
    ad = BridgeAdapter(config=AdapterConfig())
    ad.register_send_input(1, "a")
    ad.register_send_input(2, "b")
    ad.flush()
    assert ad.state.send_inputs == []


def test_adapter_crash_fallback_restart_then_silent():
    cfg = AdapterConfig(analyzer_crash_fallback="notify", analyzer_max_restarts=2)
    ad = BridgeAdapter(config=cfg)
    assert ad.on_analyzer_crash() == "restart"
    assert ad.on_analyzer_crash() == "restart"
    # 3회째부터 silent (notify policy 의 종점)
    assert ad.on_analyzer_crash() == "silent"


def test_adapter_crash_fallback_parser_mode():
    cfg = AdapterConfig(analyzer_crash_fallback="parser_fallback", analyzer_max_restarts=1)
    ad = BridgeAdapter(config=cfg)
    assert ad.on_analyzer_crash() == "restart"
    assert ad.on_analyzer_crash() == "parser_fallback"


def test_adapter_config_from_env(monkeypatch):
    monkeypatch.setenv("BRIDGE_ADAPTER_DISPATCH", "1")
    monkeypatch.setenv("BRIDGE_ADAPTER_BUSY_DISPATCH", "1")
    monkeypatch.setenv("BRIDGE_ADAPTER_BUSY_THROTTLE", "2.5")
    cfg = AdapterConfig.from_env()
    assert cfg.dispatch_enabled is True
    assert cfg.busy_dispatch is True
    assert cfg.busy_throttle_sec == 2.5
