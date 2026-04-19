"""ShadowAnalyzer — live pipe-pane raw log 을 실시간으로 tokenize 하고 분석기
파이프라인을 돌려 `analyzer-events.jsonl` 에 기록한다 (Step 4-α).

설계 요점 (step-3-design §13.4 / §13.5):
  - 기존 parser 경로 (bridge/core.py monitor) 와 **완전 분리**. 분석기 이벤트는
    frozen parser 의 `events.jsonl` 과 별도 파일.
  - 세션 offset 은 pipe-pane raw 의 누적 bytes. rotation 을 건너뛰어 이어짐.
  - rotation 은 외부에서 감지해 `switch_file(new_path, new_identity)` 로 알려주며,
    tokenizer 는 flush 후 새 파일로 전환. session_offset 은 계속 증가.
  - crash / restart 대비 `state_store` 로 (session_offset, current_path, identity)
    영속. resume 시 identity 일치 + size ≥ session_offset 이면 이어받고, 아니면
    reset (새 세션 시작으로 간주).
  - Tokenizer incomplete escape 카운트는 주기적으로 dump.event 에 노출.

공개 API:
    analyzer = ShadowAnalyzer(tmux_session="...", base=...)
    analyzer.start(current_path=Path(...))
    ...
    await analyzer.poll()            # tick 마다 호출, 파일 size 만큼 소비
    analyzer.switch_file(new_path)   # bridge 가 rotate 했을 때
    analyzer.stop()                  # monitor 종료 시
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from tools.ansi_tokenizer import Tokenizer
from tools.event_classifier import AnalyzerEvent, EventClassifier
from tools.file_identity import FileIdentity, verify_resume
from tools.line_commit import LineCommitter
from tools.region_tagger import RegionTagger
from tools.shadow_storage import JSONLAppender, open_analyzer_events
from tools.state_store import StateStore


log = logging.getLogger(__name__)


CHUNK_SIZE = 64 * 1024
STATE_FILENAME = "analyzer_state"


@dataclass
class _ShadowState:
    """영속 state — session_offset, 현재 파일 path, identity."""

    session_offset: int = 0
    current_path: str | None = None
    identity: dict | None = None  # FileIdentity.to_dict() 호환

    def to_dict(self) -> dict:
        return {
            "session_offset": self.session_offset,
            "current_path": self.current_path,
            "identity": self.identity,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "_ShadowState":
        return cls(
            session_offset=int(d.get("session_offset", 0)),
            current_path=d.get("current_path"),
            identity=d.get("identity"),
        )


@dataclass
class _RuntimeCounters:
    """관측 지표. stop 시 dump 에 기록."""

    bytes_read: int = 0
    events_emitted: int = 0
    rotations: int = 0
    tokenizer_incomplete_at_start: int = 0
    resume_resets: int = 0


class ShadowAnalyzer:
    """Live pipe-pane tail 기반 분석기. bridge/core.py 의 monitor 루프에서
    background tick 으로 호출되도록 설계됐다.

    **dispatch 는 하지 않는다** — shadow mode 전용. 이벤트는 analyzer-events.jsonl
    에만 기록.
    """

    def __init__(
        self,
        tmux_session: str,
        *,
        base: Path | None = None,
        chunk_size: int = CHUNK_SIZE,
        now: Callable[[], float] = time.monotonic,
        dump_cb: Callable[[str, str, dict], None] | None = None,
    ) -> None:
        self._tmux_session = tmux_session
        self._base = base
        self._chunk_size = chunk_size
        self._now = now
        self._dump_cb = dump_cb

        self._tokenizer = Tokenizer()
        self._committer = LineCommitter()
        self._tagger = RegionTagger()
        self._classifier = EventClassifier()

        self._state_store = StateStore(
            self._state_path(tmux_session, base=base)
        )
        self._state = _ShadowState()
        self._appender: JSONLAppender | None = None
        self._file = None
        self._file_path: Path | None = None
        self._file_identity: FileIdentity | None = None
        self._counters = _RuntimeCounters()
        self._started = False

    # -- 경로 --------------------------------------------------------------

    @staticmethod
    def _state_path(tmux_session: str, *, base: Path | None) -> Path:
        from tools.shadow_storage import session_dir
        return session_dir(tmux_session, base=base) / STATE_FILENAME

    # -- lifecycle ---------------------------------------------------------

    def start(self, *, current_path: Path | None) -> None:
        """appender 오픈 + state 복원 + current 파일 오픈.

        current_path 가 None 이면 start 를 보류 (bridge 가 pipe_attach 실패 상태).
        이후 `switch_file` 로 늦게 붙여도 된다.
        """
        if self._started:
            return
        self._appender = open_analyzer_events(
            self._tmux_session, base=self._base
        ).__enter__()

        self._load_state()
        self._started = True
        if current_path is not None:
            self._attach_file(current_path, resume=True)
        self._emit_meta("shadow_start", {
            "session_offset": self._state.session_offset,
            "current_path": str(current_path) if current_path else None,
        })

    def stop(self) -> None:
        """tokenizer/classifier flush + state 저장 + appender close."""
        if not self._started:
            return
        try:
            self._flush_pipeline()
        finally:
            self._close_file()
            self._save_state()
            self._emit_meta("shadow_stop", {
                "bytes_read": self._counters.bytes_read,
                "events_emitted": self._counters.events_emitted,
                "rotations": self._counters.rotations,
                "resume_resets": self._counters.resume_resets,
                "incomplete_escape_count": self._tokenizer.incomplete_escape_count,
                "incomplete_escape_flushed": self._tokenizer.incomplete_escape_flushed,
            })
            if self._appender is not None:
                try:
                    self._appender.__exit__(None, None, None)
                except Exception:
                    pass
                self._appender = None
            self._started = False

    # -- file swap ---------------------------------------------------------

    def switch_file(self, new_path: Path) -> None:
        """bridge 가 raw 파일을 rotate 했을 때 호출.

        현재 파일의 잔여 bytes 를 모두 소비 (best-effort) 한 뒤 새 파일로 전환.
        session_offset 은 이어진다 (rotation 은 gap 없음 가정; bridge 는 start→
        stop 간 gap 을 이미 `pipe_pane_rotate` event 로 기록).
        """
        if not self._started:
            return
        # 기존 파일 tail 까지 다 읽는다.
        self._read_until_eof()
        self._close_file()
        self._counters.rotations += 1
        self._attach_file(new_path, resume=False)

    def detach_file(self) -> None:
        """현재 파일 닫기 (tmux detach 등). session_offset/tokenizer state 유지."""
        if not self._started:
            return
        self._read_until_eof()
        self._close_file()

    # -- poll --------------------------------------------------------------

    async def poll(self) -> int:
        """현재 파일의 성장분을 소비. 읽은 byte 수 리턴.

        async 로 선언돼 있으나 내부는 동기 파일 I/O. monitor 루프가 await 문법으로
        호출하는 것과 interface 일관성 유지. blocking 시간은 chunk 하나 분량
        (<64KB, 수 ms) — tmux.capture-pane 호출 1회보다 짧다.
        """
        if not self._started or self._file is None:
            return 0
        return self._read_until_eof()

    # -- internals ---------------------------------------------------------

    def _load_state(self) -> None:
        data = self._state_store.load()
        if data:
            self._state = _ShadowState.from_dict(data)

    def _save_state(self) -> None:
        self._state_store.save(self._state.to_dict())

    def _attach_file(self, path: Path, *, resume: bool) -> None:
        """새 파일 오픈. resume=True 면 identity + size 검증 후 이어받는다."""
        ident = FileIdentity.capture(path)
        if not ident.existed:
            log.warning("shadow_analyzer: stat failed %s", path)
            self._file = None
            self._file_path = None
            self._file_identity = None
            return

        seek_to = 0
        reason: str
        if resume and self._state.identity and self._state.current_path == str(path):
            prev = FileIdentity.from_event_payload(self._state.identity)
            decision = verify_resume(
                prev, ident, last_offset=self._state.session_offset
            )
            if decision == "accept":
                seek_to = self._state.session_offset
                reason = "resume"
            else:
                reason = decision  # reset_inode_mismatch / reset_truncated / reset_missing
                self._state.session_offset = 0
                self._counters.resume_resets += 1
                self._reset_pipeline()
        else:
            reason = "new_file" if not resume else "fresh_attach"

        try:
            fh = path.open("rb")
        except OSError as e:
            log.warning("shadow_analyzer: open failed %s: %s", path, e)
            return
        try:
            if seek_to:
                fh.seek(seek_to)
        except OSError:
            fh.close()
            self._state.session_offset = 0
            self._counters.resume_resets += 1
            self._reset_pipeline()
            fh = path.open("rb")

        self._file = fh
        self._file_path = path
        self._file_identity = ident
        self._state.current_path = str(path)
        self._state.identity = {
            "path": ident.path,
            "existed": ident.existed,
            "inode": ident.inode,
            "size": ident.size,
            "mtime_ns": ident.mtime_ns,
        }
        self._counters.tokenizer_incomplete_at_start = (
            self._tokenizer.incomplete_escape_count
        )
        self._emit_meta("file_attach", {
            "path": str(path),
            "seek_to": seek_to,
            "reason": reason,
            "identity": self._state.identity,
        })

    def _close_file(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def _read_until_eof(self) -> int:
        """현재 파일에서 새로운 bytes 를 모두 소비. 리턴: 이번 호출에서 읽은 총
        byte 수."""
        if self._file is None:
            return 0
        total = 0
        while True:
            try:
                chunk = self._file.read(self._chunk_size)
            except OSError as e:
                log.warning("shadow_analyzer: read failed: %s", e)
                return total
            if not chunk:
                break
            self._ingest_chunk(chunk)
            total += len(chunk)
        if total:
            self._save_state()
        return total

    def _ingest_chunk(self, chunk: bytes) -> None:
        """chunk 를 파이프라인에 넣고 발생 이벤트 기록."""
        offset = self._state.session_offset
        events: list[AnalyzerEvent] = []
        try:
            for tok in self._tokenizer.feed(offset, chunk):
                for commit in self._committer.feed(tok):
                    tagged = self._tagger.tag(commit)
                    events.extend(self._classifier.feed(tagged))
        except AssertionError as e:
            # offset regression — 파이프라인 오염. dump 로 기록 + reset.
            self._emit_meta("pipeline_assert", {"error": str(e)})
            self._reset_pipeline()
            events = []
        self._state.session_offset = offset + len(chunk)
        self._counters.bytes_read += len(chunk)
        self._write_events(events)

    def _flush_pipeline(self) -> None:
        events: list[AnalyzerEvent] = []
        try:
            for tok in self._tokenizer.flush():
                for commit in self._committer.feed(tok):
                    tagged = self._tagger.tag(commit)
                    events.extend(self._classifier.feed(tagged))
            for commit in self._committer.flush():
                tagged = self._tagger.tag(commit)
                events.extend(self._classifier.feed(tagged))
            events.extend(self._classifier.flush())
        except AssertionError as e:
            self._emit_meta("pipeline_assert", {"error": str(e), "phase": "flush"})
        self._write_events(events)

    def _reset_pipeline(self) -> None:
        """pipeline 만 초기화. state (session_offset 등) 는 호출자가 관리."""
        self._tokenizer = Tokenizer()
        self._committer = LineCommitter()
        self._tagger = RegionTagger()
        self._classifier = EventClassifier()

    def _write_events(self, events: Iterable[AnalyzerEvent]) -> None:
        if self._appender is None:
            return
        n = 0
        for ev in events:
            try:
                self._appender.write(ev)
                n += 1
            except Exception as e:
                log.warning("shadow_analyzer: write failed: %s", e)
                return
        self._counters.events_emitted += n

    def _emit_meta(self, t: str, payload: dict) -> None:
        """shadow 운영 지표를 analyzer-events.jsonl 에 기록 + optional dump."""
        record = {
            "offset": self._state.session_offset,
            "t": f"shadow.{t}",
            "region": "meta",
            **payload,
        }
        if self._appender is not None:
            try:
                self._appender.write(record)
            except Exception as e:
                log.warning("shadow_analyzer: meta write failed: %s", e)
        if self._dump_cb is not None:
            try:
                self._dump_cb("shadow_analyzer", t, payload)
            except Exception:
                pass
