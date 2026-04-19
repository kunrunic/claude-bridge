"""BridgeAdapter — 분석기 이벤트 → Telegram dispatch 경계.

§13.1 (step-3-design.md) 명세. 현재는 **shadow 용 skeleton** — Step 4-γ cutover 전까지
실제 dispatch 는 하지 않고, 이벤트 매핑/decision 만 내부적으로 구축한다. dispatch
스위치는 `AdapterConfig.dispatch_enabled` 로 제어.

주요 책임:
- 분석기 이벤트 소비 (`process_event`).
- 이벤트 × 액션 × Telegram 매핑 (§13.1.b).
- send_input 기록 (§13.3/§13.11.c — region-based echo 억제는 분석기 측에서,
  텍스트 상관 tie-breaker 는 여기서 보조).
- BusyThrottle / boot banner suppress / approval_cancel notify.
- Analyzer crash fallback 훅 (§13.5).

본 파일은 의존성 최소화: telegram_sender / dump_writer 를 생성자로 주입 받는다.
테스트에서는 mock 주입.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

try:
    from tools.event_classifier import AnalyzerEvent
except ImportError:  # pragma: no cover — tools 패키지 미설치 환경 방어
    AnalyzerEvent = Any  # type: ignore


# -- 설정 --------------------------------------------------------------------


@dataclass
class AdapterConfig:
    """§13.1.c — dispatcher 동작 flag. 기본값은 case-04 류 회귀를 막는 쪽으로."""

    # Dispatch 전체 스위치. False 시 내부 상태만 갱신, 외부 호출 없음 (shadow).
    dispatch_enabled: bool = False

    # busy_enter → Telegram progress 메시지 갱신 간격 (초).
    busy_throttle_sec: float = 5.0

    # busy_* 를 Telegram 에 노출할지. 기본 suppress (§13.1.b U2 — 폭탄 방지).
    busy_dispatch: bool = False

    # session_boot / session_resume 노출. 기본 suppress (dump 만).
    boot_banner_dispatch: bool = False

    # approval_cancel(method="flush") 즉시 통보. 기본 True (case-04 대응).
    approval_cancel_notify: bool = True

    # Analyzer crash 시 정책: "notify" | "silent" | "parser_fallback".
    analyzer_crash_fallback: str = "notify"
    analyzer_max_restarts: int = 3

    @classmethod
    def from_env(cls) -> "AdapterConfig":
        """env override. 'BRIDGE_ADAPTER_*' prefix."""

        def _flag(name: str, default: bool) -> bool:
            v = os.getenv(name)
            if v is None:
                return default
            return v not in ("0", "false", "False", "")

        def _float(name: str, default: float) -> float:
            v = os.getenv(name)
            if v is None:
                return default
            try:
                return float(v)
            except ValueError:
                return default

        return cls(
            dispatch_enabled=_flag("BRIDGE_ADAPTER_DISPATCH", False),
            busy_throttle_sec=_float("BRIDGE_ADAPTER_BUSY_THROTTLE", 5.0),
            busy_dispatch=_flag("BRIDGE_ADAPTER_BUSY_DISPATCH", False),
            boot_banner_dispatch=_flag("BRIDGE_ADAPTER_BOOT_BANNER", False),
            approval_cancel_notify=_flag("BRIDGE_ADAPTER_APPROVAL_CANCEL_NOTIFY", True),
            analyzer_crash_fallback=os.getenv(
                "BRIDGE_ADAPTER_CRASH_FALLBACK", "notify"
            ),
            analyzer_max_restarts=int(
                os.getenv("BRIDGE_ADAPTER_MAX_RESTARTS", "3")
            ),
        )


# -- 주입 인터페이스 ---------------------------------------------------------


class _SenderLike(Protocol):
    """telegram_sender 가 제공해야 할 최소 인터페이스 (테스트 주입용)."""

    async def send_response(self, text: str, kind: str) -> None: ...
    async def send_approval(
        self, tool_hint: str, summary: str, box_text: str
    ) -> None: ...
    async def notify_cancel(self, reason: str) -> None: ...
    async def notify(self, text: str) -> None: ...
    async def notify_limit(self, message: str) -> None: ...


class _DumpLike(Protocol):
    def event(self, source: str, kind: str, **fields: Any) -> None: ...


# -- 매핑 테이블 -------------------------------------------------------------

# Action 은 enum 대신 string literal — 직렬화 편의성 우선.
# "dispatch_*" = 외부 I/O 발생, "suppress" = 내부 state 만 갱신, "hold" = 상태머신 버퍼링.
ACTION_DISPATCH_RESPONSE = "dispatch_response"
ACTION_DISPATCH_APPROVAL = "dispatch_approval"
ACTION_DISPATCH_APPROVAL_RESOLVED = "dispatch_approval_resolved"
ACTION_DISPATCH_APPROVAL_CANCEL = "dispatch_approval_cancel"
ACTION_DISPATCH_COMPACT_START = "dispatch_compact_start"
ACTION_DISPATCH_COMPACT_COMPLETE = "dispatch_compact_complete"
ACTION_DISPATCH_LIMIT = "dispatch_limit"
ACTION_DISPATCH_TRUST = "dispatch_trust"
ACTION_DISPATCH_RESUME_PICKER = "dispatch_resume_picker"
ACTION_SUPPRESS = "suppress"
ACTION_BUSY_THROTTLE = "busy_throttle"
ACTION_BUSY_CLEAR = "busy_clear"
ACTION_DISPATCH_USER_PROMPT = "dispatch_user_prompt"

# 기본 매핑 — AdapterConfig 의 flag 들이 이 결정을 오버라이드.
# (key = AnalyzerEvent.t)
_DEFAULT_ACTION_MAP: dict[str, str] = {
    "block_commit": ACTION_DISPATCH_RESPONSE,
    "approval_show": ACTION_DISPATCH_APPROVAL,
    "approval_confirm": ACTION_DISPATCH_APPROVAL_RESOLVED,
    "approval_deny": ACTION_DISPATCH_APPROVAL_RESOLVED,
    "approval_cancel": ACTION_DISPATCH_APPROVAL_CANCEL,
    "busy_enter": ACTION_BUSY_THROTTLE,
    "busy_exit": ACTION_BUSY_CLEAR,
    "session_boot": ACTION_SUPPRESS,     # boot_banner_dispatch 로 override
    "session_resume": ACTION_SUPPRESS,   # 동
    "user_prompt": ACTION_DISPATCH_USER_PROMPT,
    "user_echo": ACTION_SUPPRESS,        # 정의상 echo
    "compact_start": ACTION_DISPATCH_COMPACT_START,
    "compact_complete": ACTION_DISPATCH_COMPACT_COMPLETE,
    "limit": ACTION_DISPATCH_LIMIT,
    "trust_prompt": ACTION_DISPATCH_TRUST,
    "resume_picker": ACTION_DISPATCH_RESUME_PICKER,
}


def resolve_action(event_type: str, config: AdapterConfig) -> str:
    """이벤트 타입 + 설정 → action 결정. §13.1.b 매핑 테이블의 코드화."""
    base = _DEFAULT_ACTION_MAP.get(event_type, ACTION_SUPPRESS)

    # session_boot/resume override
    if event_type in ("session_boot", "session_resume"):
        if config.boot_banner_dispatch:
            return ACTION_DISPATCH_RESPONSE  # 일반 dispatch 경로 재사용
        return ACTION_SUPPRESS

    # busy_* override
    if event_type == "busy_enter":
        return ACTION_BUSY_THROTTLE if config.busy_dispatch else ACTION_SUPPRESS
    if event_type == "busy_exit":
        return ACTION_BUSY_CLEAR if config.busy_dispatch else ACTION_SUPPRESS

    # approval_cancel override
    if event_type == "approval_cancel":
        if not config.approval_cancel_notify:
            return ACTION_SUPPRESS
        return base

    return base


# -- 송신 시도 기록 ----------------------------------------------------------


@dataclass
class _AdapterState:
    """런타임 상태 — 외부 노출은 최소화. 테스트는 이걸 직접 검사."""

    # -inf sentinel: "아직 한 번도 dispatch 안 함". 실제 시각 값(0.0 포함)과
    # 충돌 없이 "첫 호출은 throttle 없이 통과" 의미를 표현한다.
    last_busy_dispatch_at: float = float("-inf")
    approval_active: bool = False
    # send_input 기록 — (offset, text, ts) tuple. 4KB 윈도우 안의 block_commit 검증용.
    send_inputs: list[tuple[int, str, float]] = field(default_factory=list)
    # crash 카운터
    analyzer_restarts: int = 0


# -- BridgeAdapter 본체 -------------------------------------------------------


class BridgeAdapter:
    """분석기 이벤트 → Telegram dispatch 를 **추상적으로** 결정한다.

    실제 외부 I/O 는 주입된 sender 에게 위임. dispatch_enabled=False 면 resolve 만
    하고 외부 호출은 하지 않음 (shadow run 용도).
    """

    def __init__(
        self,
        *,
        sender: _SenderLike | None = None,
        dump_writer: _DumpLike | None = None,
        config: AdapterConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or AdapterConfig()
        self.sender = sender
        self.dump_writer = dump_writer
        self._clock = clock
        self.state = _AdapterState()

    # -- public --------------------------------------------------------------

    async def process_event(self, event: AnalyzerEvent) -> str:
        """이벤트 1건 소비. 결정된 action 이름을 리턴 (테스트/관측 용도).

        주의: 실제 dispatch 는 config.dispatch_enabled=True 일 때만.
        """
        action = resolve_action(event.t, self.config)
        self._record(event, action)

        if not self.config.dispatch_enabled:
            return action

        if action == ACTION_BUSY_THROTTLE:
            now = self._clock()
            if now - self.state.last_busy_dispatch_at < self.config.busy_throttle_sec:
                return ACTION_SUPPRESS
            self.state.last_busy_dispatch_at = now
            # 실제 노출은 sender.notify
            if self.sender is not None:
                label = event.payload.get("label", "작업 중") if hasattr(event, "payload") else "작업 중"
                await self.sender.notify(f"⏳ {label}...")
            return action

        if action == ACTION_BUSY_CLEAR:
            self.state.last_busy_dispatch_at = 0.0
            return action

        if action == ACTION_SUPPRESS:
            return action

        # 이하는 실제 dispatch. sender 없으면 suppress 로 격하 (shadow mode).
        if self.sender is None:
            return ACTION_SUPPRESS

        if action == ACTION_DISPATCH_RESPONSE:
            text = event.payload.get("text", "")
            kind = event.payload.get("kind", "response")
            await self.sender.send_response(text, kind)
        elif action == ACTION_DISPATCH_APPROVAL:
            self.state.approval_active = True
            await self.sender.send_approval(
                event.payload.get("tool_hint", "unknown"),
                event.payload.get("summary", ""),
                event.payload.get("box_text", ""),
            )
        elif action == ACTION_DISPATCH_APPROVAL_RESOLVED:
            self.state.approval_active = False
            await self.sender.notify("✅ 승인 처리 완료")
        elif action == ACTION_DISPATCH_APPROVAL_CANCEL:
            method = event.payload.get("method", "")
            if method == "flush":
                await self.sender.notify_cancel("응답 스트림 종료로 승인 취소")
            self.state.approval_active = False
        elif action == ACTION_DISPATCH_COMPACT_START:
            await self.sender.notify("🗜️ 컨텍스트 압축 중...")
        elif action == ACTION_DISPATCH_COMPACT_COMPLETE:
            await self.sender.notify("✅ 압축 완료")
        elif action == ACTION_DISPATCH_LIMIT:
            await self.sender.notify_limit(event.payload.get("message", ""))
        elif action == ACTION_DISPATCH_TRUST:
            await self.sender.notify("🔐 폴더 신뢰 프롬프트 감지")
        elif action == ACTION_DISPATCH_RESUME_PICKER:
            await self.sender.notify("🔁 resume picker 감지 — 기본값 사용")
        elif action == ACTION_DISPATCH_USER_PROMPT:
            # 기본: suppress (사용자가 본인 입력을 다시 받을 필요 없음).
            # 필요 시 config extension 지점.
            pass

        return action

    def register_send_input(self, offset: int, text: str) -> None:
        """§13.3/§13.11.c — bridge 가 tmux 로 보낸 입력을 기록.

        region-based echo 억제 (§13.11.c) 가 1차이므로 여기선 tie-breaker 텍스트
        상관용 기록만 보관. 2000 entry 이상이면 오래된 것 drop.
        """
        ts = self._clock()
        self.state.send_inputs.append((offset, text, ts))
        if len(self.state.send_inputs) > 2000:
            self.state.send_inputs = self.state.send_inputs[-1000:]
        if self.dump_writer is not None:
            self.dump_writer.event(
                "adapter", "register_send_input",
                offset=offset, length=len(text),
                preview=text[:200],
            )

    def flush(self) -> None:
        """세션 종료/롤백. 현재는 내부 상태 reset 만."""
        self.state = _AdapterState()
        if self.dump_writer is not None:
            self.dump_writer.event("adapter", "flush")

    # -- internal ------------------------------------------------------------

    def _record(self, event: AnalyzerEvent, action: str) -> None:
        if self.dump_writer is None:
            return
        try:
            fields = {
                "offset": getattr(event, "offset", None),
                "event_type": getattr(event, "t", None),
                "action": action,
            }
            self.dump_writer.event("adapter", "process_event", **fields)
        except Exception:
            pass

    # -- crash fallback hook -------------------------------------------------

    def on_analyzer_crash(self) -> str:
        """analyzer 가 exception 으로 죽었을 때 호출. §13.5 policy 결정.

        Returns: "restart" | "parser_fallback" | "silent".
        """
        self.state.analyzer_restarts += 1
        policy = self.config.analyzer_crash_fallback
        if policy == "silent":
            return "silent"
        if self.state.analyzer_restarts > self.config.analyzer_max_restarts:
            if policy == "parser_fallback":
                return "parser_fallback"
            return "silent"
        return "restart"
