"""디버그 덤프 — 스트리밍 누락 추적용.

CB_DUMP=1 env 가 켜져야 동작. 꺼져있으면 모든 함수 no-op.
(start.sh --dump 플래그가 이 env 를 설정한다.)

수집 대상
--------
- pane_tick.jsonl — 0.5s 주기 tmux pane 캡처 (tick task 가 기록)
- events.jsonl    — tmux/sender/receiver/handler 에서 호출한 event() 로그

경로
----
dump/YYYYMMDD/HHMMSS_{instance}/
  ├─ pane_tick.jsonl
  └─ events.jsonl

bugreporter 가 최근 디렉토리를 증거에 자동 포함시킨다.

설계 원칙
---------
- 꺼져있을 때 오버헤드 0 (enabled 플래그 한 번만 체크)
- 이벤트 기록 실패가 봇 로직에 영향 주면 안 됨 (모두 try 로 감싸고 silent)
- pane hash-dedup: 동일 pane 이면 스킵 (idle 구간 압축). busy 중엔 타이머 초 때문에
  거의 매 tick 이 다르므로 dedup 효과 적지만, idle 에서는 확실히 효과.
- 보관 3일 → 부팅 시 오래된 디렉토리 자동 정리
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

_ENABLED: bool = bool(os.getenv("CB_DUMP"))
_TICK_INTERVAL: float = 0.5
_RETENTION_DAYS: int = 3

_ROOT: Path | None = None
_PANE_LOG: Any = None     # file handle
_EVENT_LOG: Any = None    # file handle
_LAST_PANE_HASH: str = ""
_TICK_TASK: asyncio.Task | None = None


def is_enabled() -> bool:
    return _ENABLED


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"


def _ensure_dir(instance: str) -> Path:
    """부팅 1회 — dump/YYYYMMDD/HHMMSS_instance/ 생성 및 리턴."""
    root = Path(__file__).parent.parent / "dump"
    day = time.strftime("%Y%m%d")
    stamp = time.strftime("%H%M%S")
    path = root / day / f"{stamp}_{instance}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup_old(root: Path) -> None:
    """보관 기간 초과 디렉토리 정리 (dump/YYYYMMDD/ 단위)."""
    if not root.exists():
        return
    cutoff = time.time() - _RETENTION_DAYS * 86400
    for day_dir in root.iterdir():
        if not day_dir.is_dir():
            continue
        try:
            # 디렉토리명 YYYYMMDD 로 판정 (파일 mtime 보다 확실)
            ts = time.mktime(time.strptime(day_dir.name, "%Y%m%d"))
            if ts < cutoff:
                shutil.rmtree(day_dir, ignore_errors=True)
        except (ValueError, OSError):
            continue


def init(instance: str = "default") -> None:
    """bot 부팅 시 호출. CB_DUMP 꺼져있으면 즉시 리턴."""
    global _ROOT, _PANE_LOG, _EVENT_LOG
    if not _ENABLED:
        return
    if _ROOT is not None:
        return  # 이미 초기화됨

    try:
        root = Path(__file__).parent.parent / "dump"
        _cleanup_old(root)
        _ROOT = _ensure_dir(instance)
        _PANE_LOG = open(_ROOT / "pane_tick.jsonl", "a", encoding="utf-8", buffering=1)
        _EVENT_LOG = open(_ROOT / "events.jsonl", "a", encoding="utf-8", buffering=1)
        # 시작 마커
        event("dump", "init", instance=instance, pid=os.getpid())
        print(f"[dump] recording to {_ROOT}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[dump] init failed: {e}", file=sys.stderr, flush=True)


def event(source: str, kind: str, **fields: Any) -> None:
    """이벤트 한 줄 기록.

    source: 'tmux' | 'sender' | 'receiver' | 'handler' | 'core' | 'dump'
    kind:   source 내 구체 이벤트 이름 (e.g., 'send_input', 'send_output', 'callback')
    fields: 추가 메타 (텍스트는 길면 자체 trim 권장)
    """
    if not _ENABLED or _EVENT_LOG is None:
        return
    try:
        rec = {"ts": _now_iso(), "source": source, "kind": kind}
        rec.update(fields)
        _EVENT_LOG.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _trim(text: str, max_chars: int = 2000) -> str:
    # pane 캡처의 새 블록은 **하단** 에 찍히므로 head 가 아닌 tail 을 남겨야
    # diagnostic 으로 가치가 있다. head 보존은 세션 부팅 배너 같은 이미 안정된
    # 구간만 보여줄 뿐, 최근 이벤트·incident 재분석에 블라인드 영역을 만든다.
    # (20260420_072013: `⏺ 좋은 질문` 블록이 pane_tick 187개 중 한 번도 안 잡혀
    #  근본원인 분석이 막힌 사건. 원인은 _trim 이 head 8000자만 저장했기 때문.)
    if len(text) <= max_chars:
        return text
    return f"...[+{len(text) - max_chars} chars]" + text[-max_chars:]


def pane_snapshot(raw: str, clean: str, *, busy: bool, awaiting_approval: bool) -> None:
    """pane tick 기록. hash-dedup 으로 동일 화면은 스킵."""
    global _LAST_PANE_HASH
    if not _ENABLED or _PANE_LOG is None:
        return
    try:
        h = hashlib.md5(clean.encode("utf-8", errors="replace")).hexdigest()
        if h == _LAST_PANE_HASH:
            return
        _LAST_PANE_HASH = h
        rec = {
            "ts": _now_iso(),
            "hash": h,
            "busy": busy,
            "awaiting_approval": awaiting_approval,
            "raw": _trim(raw, 8000),
            "clean": _trim(clean, 8000),
        }
        _PANE_LOG.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def _tick_loop(tmux_module, parser_module, bridge_obj) -> None:
    """0.5s 주기 pane 스냅샷 task."""
    while True:
        try:
            await asyncio.sleep(_TICK_INTERVAL)
            if not _ENABLED:
                return
            raw = await tmux_module.pane_output_async()
            clean = parser_module.strip_ansi(raw).strip()
            pane_snapshot(
                raw,
                clean,
                busy=parser_module.is_busy(clean),
                awaiting_approval=bool(getattr(bridge_obj, "awaiting_approval", False)),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            event("dump", "tick_error", error=str(e)[:200])


def start_tick(tmux_module, parser_module, bridge_obj) -> asyncio.Task | None:
    """monitor 가 시작될 때 호출. CB_DUMP 꺼져있으면 None 리턴."""
    global _TICK_TASK
    if not _ENABLED:
        return None
    if _TICK_TASK is not None and not _TICK_TASK.done():
        return _TICK_TASK
    _TICK_TASK = asyncio.create_task(_tick_loop(tmux_module, parser_module, bridge_obj))
    event("dump", "tick_started", interval_sec=_TICK_INTERVAL)
    return _TICK_TASK


def stop_tick() -> None:
    global _TICK_TASK
    if _TICK_TASK is not None and not _TICK_TASK.done():
        _TICK_TASK.cancel()
        event("dump", "tick_stopped")
    _TICK_TASK = None
