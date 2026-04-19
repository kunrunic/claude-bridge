"""File identity 캡처 + resume 검증 — step-3-design §13.4.

bridge 는 `pipe_pane_identity` (open/close) 와 `pipe_pane_rotate` 이벤트로 raw
로그의 `{inode, size, mtime_ns}` 을 dump 에 기록한다. 분석기가 나중에 resume
할 때 이 식별자를 비교해 원본이 바뀌었는지 판정한다:

    inode 일치 + size ≥ last_offset  →  accept (정상 재개)
    inode 불일치                    →  reset  (파일 교체)
    size < last_offset              →  reset  (truncate 또는 교체)

본 모듈은 의도적으로 bridge import 를 피해 offline 분석기 환경에서도 사용 가능하다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class FileIdentity:
    path: str
    existed: bool
    inode: int | None = None
    size: int | None = None
    mtime_ns: int | None = None

    @classmethod
    def capture(cls, path: Path | str) -> "FileIdentity":
        p = Path(path)
        try:
            st = p.stat()
        except OSError:
            return cls(path=str(p), existed=False)
        return cls(
            path=str(p),
            existed=True,
            inode=st.st_ino,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
        )

    @classmethod
    def from_event_payload(cls, payload: dict) -> "FileIdentity":
        return cls(
            path=payload.get("path", ""),
            existed=bool(payload.get("existed", False)),
            inode=payload.get("inode"),
            size=payload.get("size"),
            mtime_ns=payload.get("mtime_ns"),
        )


ResumeDecision = Literal["accept", "reset_inode_mismatch", "reset_truncated",
                          "reset_missing"]


def verify_resume(
    expected: FileIdentity, current: FileIdentity, *, last_offset: int
) -> ResumeDecision:
    """§13.4 resume 검증 규칙.

    - `expected` 는 이전 run 마지막 기록 (예: pipe_pane_identity phase=close).
    - `current` 는 분석기가 지금 stat 한 결과.
    - `last_offset` 은 분석기가 마지막으로 커밋한 세션 오프셋.

    inode 가 `expected` / `current` 중 한 쪽이라도 없으면 동일성 입증 불가 →
    `reset_missing` 을 반환해 보수적 reset 유도.
    """
    if not current.existed:
        return "reset_missing"
    if expected.inode is None or current.inode is None:
        return "reset_missing"
    if expected.inode != current.inode:
        return "reset_inode_mismatch"
    if current.size is None or current.size < last_offset:
        return "reset_truncated"
    return "accept"


def should_reset(decision: ResumeDecision) -> bool:
    return decision != "accept"
