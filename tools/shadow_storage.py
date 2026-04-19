"""Shadow run storage — step-3-design §13.5 (C5).

Shadow run 동안 analyzer 이벤트와 shadow-diff 결과를 **bridge 의 events.jsonl 과
분리된 파일** 에 기록한다. bridge 의 events.jsonl 은 parser 전용으로 유지
(frozen parser 보호).

경로 규칙 (raw 로그와 같은 디렉토리):
    ~/.claude-bridge/panes/<tmux_session>/analyzer-events.jsonl
    ~/.claude-bridge/panes/<tmux_session>/shadow-diff.jsonl

cutover 시 rename 을 쉽게 하기 위해 모든 shadow 파일은 동일 디렉토리에 모은다.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_BASE_DIR = Path.home() / ".claude-bridge" / "panes"


def session_dir(tmux_session: str, *, base: Path | None = None) -> Path:
    """세션별 shadow 저장 디렉토리. 존재 보장은 writer 에서."""
    return (base or DEFAULT_BASE_DIR) / tmux_session


def analyzer_events_path(tmux_session: str, *, base: Path | None = None) -> Path:
    return session_dir(tmux_session, base=base) / "analyzer-events.jsonl"


def shadow_diff_path(tmux_session: str, *, base: Path | None = None) -> Path:
    return session_dir(tmux_session, base=base) / "shadow-diff.jsonl"


def _to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        # AnalyzerEvent 는 to_dict() 를 가지지만, fallback 으로 asdict 사용.
        to_dict = getattr(obj, "to_dict", None)
        if callable(to_dict):
            return to_dict()
        return asdict(obj)
    return obj


class JSONLAppender:
    """append-only JSONL writer.

    각 write 는 즉시 flush 하고, close 시 fsync 한다. 프로세스가 중간에
    죽어도 이미 write 된 라인은 보존되고 부분 라인은 없다 (append atomic
    by POSIX convention with single-writer line-sized writes).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh = None

    @property
    def path(self) -> Path:
        return self._path

    def __enter__(self) -> "JSONLAppender":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def write(self, record: Any) -> None:
        if self._fh is None:
            raise RuntimeError("JSONLAppender used outside of `with` block")
        self._fh.write(
            json.dumps(_to_jsonable(record), ensure_ascii=False) + "\n"
        )
        self._fh.flush()

    def write_many(self, records: Iterable[Any]) -> int:
        n = 0
        for r in records:
            self.write(r)
            n += 1
        return n

    def close(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except OSError:
            # fsync 실패는 치명적이지 않음 (최신 write 만 risk).
            pass
        self._fh.close()
        self._fh = None


def open_analyzer_events(
    tmux_session: str, *, base: Path | None = None
) -> JSONLAppender:
    return JSONLAppender(analyzer_events_path(tmux_session, base=base))


def open_shadow_diff(
    tmux_session: str, *, base: Path | None = None
) -> JSONLAppender:
    return JSONLAppender(shadow_diff_path(tmux_session, base=base))


def cutover_rename(
    tmux_session: str, *, base: Path | None = None
) -> dict:
    """Step 4-γ cutover: analyzer-events.jsonl → (유지), shadow-diff 는 archive.

    rollback 필요 시 `rollback_rename` 호출. 현재는 shadow-diff 만 `.archived-N`
    접미로 이동하고 analyzer-events 는 그대로 둔다 (§13.5).

    Returns: {"archived": [...]} — 이동된 파일 목록.
    """
    d = session_dir(tmux_session, base=base)
    archived: list[str] = []
    sd = shadow_diff_path(tmux_session, base=base)
    if sd.exists():
        n = 0
        while True:
            candidate = d / f"shadow-diff.archived-{n}.jsonl"
            if not candidate.exists():
                break
            n += 1
        sd.rename(candidate)
        archived.append(str(candidate))
    return {"archived": archived}
