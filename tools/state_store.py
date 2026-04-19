"""Atomic JSON state store with 2-version durability — step-3-design §13.4.

분석기 state 파일이 SIGKILL, OOM, 또는 디스크 이슈로 쓰기 도중 잘려도 안전하게
이전 버전으로 복구되도록:

    save(data):
        1. tmpfile 에 JSON 직렬화 + fsync
        2. 현재 .current 가 있으면 .prev 로 이동 (이전 2-version)
        3. tmpfile → .current 로 atomic rename
        4. 디렉토리 fsync (directory entry 동기화, POSIX)

    load():
        1. .current 먼저 시도 → JSON 파싱 성공이면 반환
        2. 실패 / 없음 → .prev 시도
        3. 둘 다 실패면 None

동일 디렉토리 내 rename 은 POSIX 에서 atomic 이므로 `.current` 가 항상 완전한
파일을 가리키도록 보장된다. 2-version 유지로 `.current` 가 어떤 이유로 소실돼도
`.prev` 로 fallback 가능.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class StateStore:
    """2-version atomic JSON store.

    base_path 가 `/x/y/analyzer_state` 이면 파일은 `/x/y/analyzer_state.current`
    와 `/x/y/analyzer_state.prev`.
    """

    def __init__(self, base_path: Path | str) -> None:
        self._base = Path(base_path)
        self._current = Path(str(self._base) + ".current")
        self._prev = Path(str(self._base) + ".prev")

    @property
    def current_path(self) -> Path:
        return self._current

    @property
    def prev_path(self) -> Path:
        return self._prev

    def save(self, data: Any) -> None:
        """atomic JSON write. 기존 .current 는 .prev 로 rotate."""
        self._base.parent.mkdir(parents=True, exist_ok=True)

        # 1) tmpfile 에 써서 fsync
        fd, tmp_name = tempfile.mkstemp(
            dir=self._base.parent,
            prefix=self._base.name + ".",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        # 2) 현재 .current → .prev (있으면)
        if self._current.exists():
            # replace 로 이전 .prev 덮어씀 — atomic
            os.replace(self._current, self._prev)

        # 3) tmp → .current atomic rename
        os.replace(tmp_path, self._current)

        # 4) directory fsync — dentry 동기화로 내구성 보장
        try:
            dir_fd = os.open(self._base.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # 일부 플랫폼 (가령 macOS 에서 tmpfs) 에서 O_DIRECTORY / dir fsync
            # 미지원일 수 있음 — 그 경우 rename atomicity 만으로 허용.
            pass

    def load(self) -> Any | None:
        """최우선 .current, 실패 시 .prev fallback. 둘 다 없으면 None."""
        for candidate in (self._current, self._prev):
            if not candidate.exists():
                continue
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
        return None

    def clear(self) -> None:
        """두 버전 모두 삭제. 테스트 / reset 용."""
        for p in (self._current, self._prev):
            p.unlink(missing_ok=True)
