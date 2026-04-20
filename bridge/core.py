"""Bridge 오케스트레이터 + monitor 루프.

Telegram ↔ Claude(tmux) 중계의 중심. receiver 에서 호출하는 start/stop 과
자체 수행하는 monitor() 루프로 이루어진다.

sibling 모듈 호출은 `<module>.<name>` 형태로 접근해 테스트 패치 지점을
단일 위치 (`bridge.<module>.<name>`) 로 고정한다.

전달 모델
--------
content-hash dedup 을 제거하고 position-based `StreamQueue` 로 통일한다.
    pane → 완료된 ⏺ 블록 추출 → queue.take_new 로 새 블록만 얻음
         → 전송 성공 시 queue.advance(idx+sent)

"전송 = 소비" 원칙. 동일 문자열이 우연히 다시 등장해도 재전송하지 않는다.
idx 는 monitor 내부에서만 전진하며, 사용자가 새 turn 을 시작할 때
`receiver.py` 측에서 `bridge.queue.reset()` 을 불러 0 으로 되돌린다.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import time
from pathlib import Path

from telegram.ext import Application

from . import config, dump, parser, sender, session, tmux
from .shadow_analyzer import ShadowAnalyzer
from .config import (
    BUSY_STREAM_SEC,
    BUSY_STUCK_SEC,
    BUSY_TIMEOUT_SEC,
    CLAUDE,
    SETTLE_TICKS,
    STOP_WAIT_SEC,
    _log,
)
from .stream_queue import StreamQueue


class Bridge:
    def __init__(self):
        self.chat_id: int | None = None
        self.running: bool = False
        self.task: asyncio.Task | None = None
        self.last_hash: str = ""
        self.awaiting_approval: bool = False
        self.skip_permissions: bool = False
        self.current_session_id: str | None = None
        self.last_approval_summary: str = ""
        self.last_approval_context: str = ""   # P2-1: 도구명 + 요약
        self.last_approval_full: str = ""      # P2-1: pane 전체 내용 (재연결 시 전달용)
        self.queue: StreamQueue = StreamQueue()  # ⏺ 블록 position 커서
        self.trust_ack_pending: bool = False     # 신뢰 프롬프트 auto-ack 상태
        self.resume_picker_ack_pending: bool = False  # resume summary/full 피커 auto-ack
        self.boot_notified: bool = False         # 부팅 후 사용자에게 최소 1번 이상 전달됐는지
        self.limit_reported: bool = False        # 한도 초과 알림 중복 방지
        self._state_lock = asyncio.Lock()        # P1-4: 상태 직렬화
        # queue + tmux 입력 원자성 보호 — on_message / on_callback 의 reset·send_input
        # 시퀀스가 monitor 의 _flush_completed (take_new→await send→advance_past)
        # 중간에 끼어들면 idx/last_fp 가 불일치해 블록이 stranded 되거나 재전송.
        # (20260420_072013: msg1→msg2 연속 전송 race 대응.)
        self._queue_lock = asyncio.Lock()
        # pipe-pane raw 로그 (Step 1 PoC) — monitor 진입 시 start, 종료 시 stop.
        # 본 필드는 관찰 데이터 수집용이며 기존 분석 경로에 영향 주지 않는다.
        self._pipe_log_path: Path | None = None
        self._pipe_last_size_check: float = 0.0
        # Shadow run 분석기 (Step 4-α). opt-in via BRIDGE_SHADOW_ANALYZER=1.
        # shadow 모드는 관찰 전용 — Telegram dispatch 경로에 영향 없음.
        self._shadow: ShadowAnalyzer | None = None

    # -- 세션 관리 ---------------------------------------------------------

    def _build_cmd(self, session_id: str | None) -> str:
        parts = [CLAUDE]
        if session_id:
            parts += ["--resume", session_id]
        if self.skip_permissions:
            parts.append("--dangerously-skip-permissions")
        return " ".join(parts)

    def _spawn(self, session_id: str | None) -> bool:
        """tmux 새 세션에 Claude 실행 + remain-on-exit 옵션."""
        claude_cmd = self._build_cmd(session_id)

        default_cwd = str(Path(__file__).parent.parent)
        cwd = session.get_session_cwd(session_id) if session_id else default_cwd
        if not cwd:
            cwd = default_cwd
        wrapped = f"cd {cwd!r} && {claude_cmd}"

        r = tmux.tmux_run([
            "new-session", "-d", "-s", config.TMUX,
            "-x", "80", "-y", "50",
            "sh", "-c", wrapped,
        ])
        if r.returncode != 0:
            return False
        tmux.tmux_run(["set-option", "-t", tmux._ts(), "remain-on-exit", "on"])
        return True

    def is_alive(self) -> bool:
        """tmux 세션에 실행 중인 프로세스가 살아있는지."""
        r = tmux.tmux_run(["list-panes", "-t", tmux._ts(), "-F", "#{pane_dead}"])
        if r.returncode != 0:
            return False
        return r.stdout.strip() == "0"

    async def is_alive_async(self) -> bool:
        r = await tmux.tmux_run_async(["list-panes", "-t", tmux._ts(), "-F", "#{pane_dead}"])
        if r.returncode != 0:
            return False
        return r.stdout.strip() == "0"

    def start(self, session_id: str | None = None, chat_id: int = 0) -> bool:
        if session_id and not session._acquire_lock(session_id, chat_id):
            return False
        session._release_lock(self.current_session_id)
        self.current_session_id = session_id
        tmux.tmux_run(["kill-session", "-t", tmux._ts()])
        return self._spawn(session_id)

    def restart(self) -> bool:
        self.running = False
        if self.task:
            self.task.cancel()
        tmux.tmux_run(["kill-session", "-t", tmux._ts()])
        return self._spawn(self.current_session_id)

    def perm_label(self) -> str:
        if self.skip_permissions:
            return "[권한 스킵 ON]  탭하면 OFF"
        return "[권한 확인 ON]  탭하면 스킵"

    # -- pipe-pane raw 로그 (Step 1 PoC) -----------------------------------

    def _new_pipe_log_path(self) -> Path:
        """세션 attach / rotate 시점마다 새 파일명 생성.

        경로: ~/.claude-bridge/panes/<tmux_session>/raw-<YYYYMMDD_HHMMSS>.log
        """
        stamp = time.strftime("%Y%m%d_%H%M%S")
        directory = config.BRIDGE_PIPE_PANE_DIR / config.TMUX
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"raw-{stamp}.log"

    def _file_identity(self, path: Path) -> dict:
        """§13.4 File identity — {inode, size, mtime_ns}. stat 실패 시 빈 dict.

        raw 로그는 shell `cat >> path` 가 비동기로 생성하므로 start 직후에는
        stat 가 OSError 를 낼 수 있다. 그 경우 existed=False 로 기록해 resume 가
        "원본이 바뀌었나" 를 구별할 수 있게 한다.
        """
        try:
            st = path.stat()
        except OSError:
            return {"existed": False, "path": str(path)}
        return {
            "existed": True,
            "path": str(path),
            "inode": st.st_ino,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
        }

    def _disk_precheck(self) -> tuple[bool, int]:
        """§13.4 pre-flight disk check.

        BRIDGE_PIPE_PANE_DIR 의 가용 공간이 MIN_FREE_MB 미만이면 (False, free_mb).
        디렉토리 미존재 / stat 실패는 통과로 간주 (best-effort).
        """
        directory = config.BRIDGE_PIPE_PANE_DIR / config.TMUX
        try:
            directory.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(directory)
        except OSError:
            return True, -1
        free_mb = usage.free // (1024 * 1024)
        return free_mb >= config.BRIDGE_PIPE_PANE_MIN_FREE_MB, free_mb

    async def _pipe_attach(self) -> None:
        """monitor 진입 시 raw 로그 수집 시작. 실패는 silent — loop 는 계속."""
        if not config.BRIDGE_PIPE_PANE_ENABLED:
            return
        ok_disk, free_mb = self._disk_precheck()
        if not ok_disk:
            dump.event(
                "core", "pipe_pane_disk_precheck_fail",
                free_mb=free_mb,
                min_free_mb=config.BRIDGE_PIPE_PANE_MIN_FREE_MB,
                dir=str(config.BRIDGE_PIPE_PANE_DIR / config.TMUX),
            )
            _log(
                "PIPE-PANE-DISK-LOW",
                f"free={free_mb}MB < min={config.BRIDGE_PIPE_PANE_MIN_FREE_MB}MB; "
                f"raw 로그 수집 건너뜀",
            )
            self._pipe_log_path = None
            self._pipe_last_size_check = time.time()
            return
        path = self._new_pipe_log_path()
        ok = await tmux.start_pipe_pane(str(path))
        self._pipe_log_path = path if ok else None
        self._pipe_last_size_check = time.time()
        if ok:
            dump.event(
                "core", "pipe_pane_identity",
                phase="open",
                **self._file_identity(path),
            )
        self._shadow_start(path if ok else None)

    async def _pipe_detach(self) -> None:
        """monitor 종료 시 pipe 해제."""
        if not config.BRIDGE_PIPE_PANE_ENABLED:
            return
        final_path = self._pipe_log_path
        self._shadow_stop()
        try:
            await tmux.stop_pipe_pane()
        finally:
            if final_path is not None:
                dump.event(
                    "core", "pipe_pane_identity",
                    phase="close",
                    **self._file_identity(final_path),
                )
            self._pipe_log_path = None

    # -- Shadow analyzer (Step 4-α) ---------------------------------------

    def _shadow_start(self, current_path: Path | None) -> None:
        """opt-in shadow run. pipe attach 성공 여부와 무관하게 호출;
        current_path=None 이면 analyzer 내부에서 no-op 으로 대기."""
        if not config.BRIDGE_SHADOW_ANALYZER:
            return
        try:
            self._shadow = ShadowAnalyzer(
                config.TMUX,
                dump_cb=lambda src, t, payload: dump.event(src, t, **payload),
            )
            self._shadow.start(current_path=current_path)
        except Exception as e:
            _log("SHADOW-START-ERROR", str(e))
            self._shadow = None

    def _shadow_stop(self) -> None:
        if self._shadow is None:
            return
        try:
            self._shadow.stop()
        except Exception as e:
            _log("SHADOW-STOP-ERROR", str(e))
        self._shadow = None

    def _shadow_switch(self, new_path: Path) -> None:
        if self._shadow is None:
            return
        try:
            self._shadow.switch_file(new_path)
        except Exception as e:
            _log("SHADOW-SWITCH-ERROR", str(e))

    async def _shadow_poll(self) -> None:
        if self._shadow is None:
            return
        try:
            await self._shadow.poll()
        except Exception as e:
            _log("SHADOW-POLL-ERROR", str(e))

    async def _pipe_maybe_rotate(self) -> None:
        """주기적으로 현재 로그 크기 확인 → 20MB 초과 시 새 파일로 교체."""
        if not config.BRIDGE_PIPE_PANE_ENABLED or self._pipe_log_path is None:
            return
        now = time.time()
        if now - self._pipe_last_size_check < config.BRIDGE_PIPE_PANE_ROTATE_CHECK_SEC:
            return
        self._pipe_last_size_check = now
        try:
            size = self._pipe_log_path.stat().st_size
        except OSError:
            return
        if size < config.BRIDGE_PIPE_PANE_MAX_BYTES:
            return
        # §13.4 — rotate 직전에도 disk 재확인. 회전이 가득 찬 디스크로 파일을
        # 새로 만들면 pipe-pane 이 쓰기 실패로 조용히 죽을 수 있다.
        ok_disk, free_mb = self._disk_precheck()
        if not ok_disk:
            dump.event(
                "core", "pipe_pane_rotate_skip_disk",
                free_mb=free_mb,
                min_free_mb=config.BRIDGE_PIPE_PANE_MIN_FREE_MB,
                current=str(self._pipe_log_path),
            )
            _log(
                "PIPE-PANE-ROTATE-SKIP",
                f"free={free_mb}MB < min={config.BRIDGE_PIPE_PANE_MIN_FREE_MB}MB; "
                f"회전 보류 (현재 파일 계속 사용)",
            )
            return
        old = self._pipe_log_path
        old_identity = self._file_identity(old)
        new_path = self._new_pipe_log_path()
        ok = await tmux.start_pipe_pane(str(new_path))
        if ok:
            self._pipe_log_path = new_path
            dump.event(
                "core", "pipe_pane_rotate",
                from_path=str(old), to_path=str(new_path),
                old_bytes=size,
                old_identity=old_identity,
                new_identity=self._file_identity(new_path),
            )
            self._shadow_switch(new_path)

    # -- flush 헬퍼 --------------------------------------------------------

    def _completed_blocks(self, clean: str, *, include_last: bool) -> list[str]:
        """pane 에서 '완료된' ⏺ 블록만 추출.

        include_last=False: 마지막 블록은 아직 성장 중일 수 있어 제외 (busy-stream).
        _is_block_active 로 'Running…/Waiting…' 블록도 빠짐없이 걸러낸다.
        """
        blocks = parser.extract_response_blocks(clean)
        if not blocks:
            return []
        candidates = blocks if include_last else blocks[:-1]
        return [b for b in candidates if not parser._is_block_active(b)]

    async def _flush_completed(
        self,
        app: Application,
        chat_id: int,
        clean: str,
        *,
        include_last: bool,
        log_tag: str,
    ) -> int:
        """_flush_completed_locked 를 _queue_lock 안에서 실행하는 래퍼."""
        async with self._queue_lock:
            return await self._flush_completed_locked(
                app, chat_id, clean,
                include_last=include_last, log_tag=log_tag,
            )

    async def _flush_completed_locked(
        self,
        app: Application,
        chat_id: int,
        clean: str,
        *,
        include_last: bool,
        log_tag: str,
    ) -> int:
        """완료된 ⏺ 블록 중 아직 큐에서 소비되지 않은 것만 push.
        리턴: 성공적으로 전송된 블록 수.
        호출자는 `_queue_lock` 을 반드시 잡은 상태여야 한다.
        """
        if self.awaiting_approval:
            return 0
        completed = self._completed_blocks(clean, include_last=include_last)
        slip = self.queue.detect_slip(completed)
        if slip is not None:
            _log("QUEUE-SLIP",
                 f"{log_tag}: {slip} idx={self.queue.idx} cc={len(completed)}")
            dump.event(
                "core", "queue_slip",
                tag=log_tag, slip_kind=slip,
                idx=self.queue.idx, cc=len(completed),
                last_fp=self.queue.last_fp,
            )
        new_blocks = self.queue.take_new(completed)
        if not new_blocks and log_tag == "response":
            # 응답 구간인데 보낼 블록 0 — stale boundary 오탐 or queue idx
            # 어긋남 의심. 연속 발생 시 alert 필요. (20260418_094744)
            _log("FLUSH-EMPTY", f"{log_tag} completed={len(completed)} idx={self.queue.idx}")
        dump.event(
            "core", "flush_peek",
            tag=log_tag,
            include_last=include_last,
            completed_count=len(completed),
            new_count=len(new_blocks),
            queue_idx=self.queue.idx,
            slip=slip,
            raw_log_path=(str(self._pipe_log_path) if self._pipe_log_path else None),
        )
        sent = 0
        for blk in new_blocks:
            _log("AI→BOT", f"{log_tag} ({len(blk)} chars)")
            dump.event(
                "core", "flush_block",
                tag=log_tag,
                length=len(blk),
                preview=blk[:500],
            )
            try:
                await sender._send_output(app, chat_id, blk)
                _log("BOT→USER", f"delivered ({log_tag})")
                sent += 1
            except Exception as e:
                _log("FLUSH-SEND-FAIL", f"{log_tag}: {e}")
                dump.event("core", "flush_send_fail", tag=log_tag, error=str(e)[:200])
                break
        if sent > 0:
            last_sent = new_blocks[sent - 1]
            self.queue.advance_past(completed, last_sent)
            self.queue.mark_sent(last_sent)
            self.boot_notified = True
        return sent

    async def _emergency_flush_before_input(
        self, app: Application, chat_id: int
    ) -> bool:
        """신규 사용자 입력을 Claude 에 주입하기 **직전** 한 번 flush.

        이유: 사용자가 메시지를 빠르게 연속으로 보낼 때, 이전 turn 의 완료 블록이
        `extract_response_blocks` 의 "마지막 ❯ 앵커" 너머로 밀려 stranded 될 수
        있다. 새 입력을 주입하기 전에 현재 pane 기준으로 한 번 더 flush 해 두면
        이 race 창을 최소화한다. (20260420_072013 incident.)

        호출자는 `_queue_lock` 을 잡은 상태에서 호출해야 한다 (wrapper 는 동일 lock
        을 재귀 시도해 교착 발생).

        반환: flush 시도 성공 True / 예외 False. False 면 호출자는 `queue.reset()`
        을 건너뛰어 기존 idx/last_fp 를 보존하는 것이 stranded 재발 방지에 안전.
        """
        if not self.running or self.task is None or self.task.done():
            return False
        try:
            raw = await tmux.pane_output_async()
            clean = parser.strip_ansi(raw).strip()
            await self._flush_completed_locked(
                app, chat_id, clean,
                include_last=True, log_tag="emergency-pre-input",
            )
            return True
        except Exception as e:
            _log("EMERGENCY-FLUSH-FAIL", str(e))
            dump.event("core", "emergency_flush_fail", error=str(e)[:200])
            return False

    # -- 종료 --------------------------------------------------------------

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
        # 내 세션이 실제로 존재할 때만 /exit 보내고 kill 한다.
        # (prefix-match 로 엉뚱한 sibling 세션을 건드리는 사고 방지)
        if tmux.tmux_run(["has-session", "-t", tmux._ts()]).returncode == 0:
            tmux.send_input("/exit")
            await asyncio.sleep(STOP_WAIT_SEC)
            tmux.tmux_run(["kill-session", "-t", tmux._ts()])
        else:
            _log("STOP", f"session '{config.TMUX}' already gone — skip /exit+kill")
        for sid, _ in session._my_locks():
            session._release_lock(sid)
        self.current_session_id = None

    # -- watchdog ----------------------------------------------------------

    async def _busy_watchdog(
        self,
        app: Application,
        chat_id: int,
        clean: str,
        *,
        reason: str,
    ) -> None:
        """busy 상태가 비정상적으로 오래 지속되거나 화면이 얼어붙었을 때 호출.
        pane 에서 아직 소비되지 않은 완료 블록을 전부 복구 전송하고,
        없으면 재전송 안내. 어떤 경우든 busy state 를 강제로 리셋한다.
        """
        _log("AI-WATCHDOG", f"trigger={reason}")

        if self.status_msg_id:
            try:
                await app.bot.delete_message(chat_id, self.status_msg_id)
            except Exception:
                pass
            self.status_msg_id = None
            self._last_status_text = ""

        # peek: 보낼 게 있는지 먼저 확인하고 안내 메시지를 그에 맞춰 송출
        completed = self._completed_blocks(clean, include_last=True)
        has_new = bool(self.queue.take_new(completed))

        if has_new:
            try:
                await app.bot.send_message(chat_id, f"⚠️ {reason} — 직전 응답 복구")
            except Exception:
                pass
            await self._flush_completed(
                app, chat_id, clean,
                include_last=True, log_tag="watchdog",
            )
        else:
            try:
                await app.bot.send_message(
                    chat_id,
                    f"⚠️ {reason}. 응답을 복구하지 못했습니다. 메시지를 다시 보내주세요.",
                )
            except Exception:
                pass

        self.was_busy = False
        self.busy_started_at = None
        self.busy_pane_hash = ""
        self.busy_last_change_at = None
        self.saw_compaction = False
        self.last_hash = hashlib.md5(clean.encode()).hexdigest()

    # -- monitor 루프 ------------------------------------------------------

    async def monitor(self, app: Application, chat_id: int):
        self.chat_id = chat_id
        self.running = True
        self.last_hash = ""
        self.queue.reset()
        self.boot_notified = False
        self.trust_ack_pending = False
        self.resume_picker_ack_pending = False
        dump.event("core", "monitor_start", chat_id=chat_id, session_id=self.current_session_id)
        dump.start_tick(tmux, parser, self)
        await self._pipe_attach()
        self.dead_reported = False
        self.was_busy = False
        self.status_msg_id: int | None = None
        self.busy_started_at: float | None = None
        self.last_status_label = ""
        self.saw_compaction: bool = False       # P1: busy 도중 압축 감지 플래그
        self._pre_busy_had_compact: bool = False # busy 진입 시점의 잔존 압축 마커 snapshot
        self.busy_pane_hash: str = ""            # P2: busy 중 pane 내용 hash
        self.busy_last_change_at: float | None = None  # P2: pane 마지막 변경 시각
        self.auto_compacting: bool = False       # Context limit 자동 /compact 진행 중
        self.compact_error_halted: bool = False  # /compact 실패 감지 → 재시도 중단
        self.busy_last_stream_at: float | None = None  # bypass 모드 중 ⏺ 스트리밍 throttle
        settle = 0
        pending = ""
        error_count = 0   # P1-6: 연속 오류 카운터

        # 부팅 시점에 pane 에 이미 있던 ⏺ 블록은 "이미 사용자가 본 것" 으로 간주.
        # queue.seed 로 해당 개수만큼 앞으로 돌려 take_new 가 재전송하지 않게 하고,
        # last_hash 도 고정해 settle 파이프라인이 즉시 발동하지 않게 한다.
        try:
            seed_clean = parser.strip_ansi(await tmux.pane_output_async()).strip()
            seed_blocks = parser.extract_response_blocks(seed_clean)
            if seed_blocks:
                self.queue.seed(len(seed_blocks))
                self.queue.mark_sent(seed_blocks[-1])
                self.last_hash = hashlib.md5(seed_clean.encode()).hexdigest()
                _log("BOOT-SEED", f"⏺ 블록 {len(seed_blocks)}개 consumed — queue.idx={self.queue.idx}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log("BOOT-SEED-ERROR", str(e))

        try:
            while self.running:
                try:
                    await self._pipe_maybe_rotate()
                    await self._shadow_poll()

                    check = await tmux.tmux_run_async(["has-session", "-t", tmux._ts()])
                    if check.returncode != 0:
                        if not self.dead_reported:
                            _log("MONITOR", f"tmux session '{config.TMUX}' gone — exiting")
                            await app.bot.send_message(chat_id, "tmux 세션이 사라졌습니다. /start로 다시 시작하세요.")
                            self.dead_reported = True
                            self.running = False
                        break

                    if not await self.is_alive_async():
                        if not self.dead_reported:
                            _log("MONITOR", "Claude process dead — exiting")
                            out = await tmux.pane_output_async()
                            clean = parser.strip_ansi(out).strip()
                            await app.bot.send_message(
                                chat_id,
                                f"Claude 프로세스가 종료되었습니다.\n마지막 출력:\n```\n{clean[-1500:]}\n```",
                                parse_mode="Markdown"
                            )
                            self.dead_reported = True
                            self.running = False
                        break

                    out = await tmux.pane_output_async()
                    clean = parser.strip_ansi(out).strip()

                    # 승인/신뢰 프롬프트는 busy 보다 우선 체크
                    is_trust = parser.is_trust_prompt(clean)
                    if is_trust:
                        if not self.trust_ack_pending:
                            tmux.send_key("Enter")
                            _log("AUTO-ACK", "trust prompt")
                            await app.bot.send_message(chat_id, "폴더 신뢰 프롬프트 자동 승인")
                            self.trust_ack_pending = True
                        await asyncio.sleep(1)
                        continue
                    elif self.trust_ack_pending:
                        self.trust_ack_pending = False

                    # resume summary/full 피커 — 기본값(summary) 으로 자동 Enter
                    if parser.is_resume_picker(clean):
                        if not self.resume_picker_ack_pending:
                            tmux.send_key("Enter")
                            _log("AUTO-ACK", "resume picker (summary)")
                            await app.bot.send_message(
                                chat_id, "Resume 피커 자동 승인 (summary)"
                            )
                            self.resume_picker_ack_pending = True
                        await asyncio.sleep(1)
                        continue
                    elif self.resume_picker_ack_pending:
                        self.resume_picker_ack_pending = False

                    # 사용량 한도 초과 감지
                    if parser.LIMIT_RE.search(clean):
                        if not self.limit_reported:
                            reset_match = re.search(
                                r"resets\s+(\d+(?::\d+)?(?:am|pm)?)\s*\(([^)]+)\)",
                                clean, re.IGNORECASE
                            )
                            reset_info = (
                                f"{reset_match.group(1)} ({reset_match.group(2)})"
                                if reset_match else None
                            )
                            msg = "⚠️ Claude 사용량 한도에 도달했습니다."
                            if reset_info:
                                msg += f"\n{reset_info}에 리셋됩니다. 그때 다시 보내주세요."
                            _log("AI-LIMIT", reset_info or "한도 초과")
                            await app.bot.send_message(chat_id, msg)
                            self.limit_reported = True
                        await asyncio.sleep(30)
                        continue

                    if self.limit_reported and not parser.LIMIT_RE.search(clean):
                        self.limit_reported = False

                    # /compact 실패 감지 — API 에러 (1M 컨텍스트 Extra Usage 미활성 등).
                    # 자동 재dispatch 루프를 막고 사용자가 /model 로 전환하도록 버튼 안내.
                    if parser.has_compaction_error(clean):
                        if not self.compact_error_halted:
                            self.compact_error_halted = True
                            self.auto_compacting = False
                            _log("AI-COMPACT-ERR", "halt auto-compact; prompt model switch")
                            try:
                                await sender._send_model_switch_prompt(app, chat_id)
                            except Exception as e:
                                _log("MODEL-SWITCH-SEND-FAIL", str(e))
                        await asyncio.sleep(2)
                        continue

                    # 에러가 pane 에서 사라지고 limit 도 해제됐으면 halted 플래그 복구
                    if (self.compact_error_halted
                            and not parser.has_compaction_error(clean)
                            and not parser.has_context_limit(clean)):
                        self.compact_error_halted = False
                        _log("AI-COMPACT-ERR", "resolved (limit cleared)")

                    # Context limit 자동 대응 — Claude Code 가 입력을 거부하므로
                    # 봇이 /compact 를 대신 dispatch 해서 흐름을 풀어준다.
                    if parser.has_context_limit(clean) and not self.compact_error_halted:
                        if not self.auto_compacting:
                            self.auto_compacting = True
                            _log("AI-CTX-LIMIT", "dispatching /compact")
                            try:
                                await app.bot.send_message(
                                    chat_id,
                                    "⚠️ Context limit 도달 — 자동 압축 실행 중. "
                                    "완료되면 메시지를 다시 보내주세요."
                                )
                            except Exception:
                                pass
                            try:
                                tmux.send_input("/compact")
                            except Exception as e:
                                _log("CTX-LIMIT-SEND-FAIL", str(e))
                        await asyncio.sleep(2)
                        continue

                    # 자동 압축 해제: limit 흔적이 사라지고 busy 도 아닐 때
                    if self.auto_compacting and not parser.is_busy(clean):
                        self.auto_compacting = False
                        _log("AI-CTX-LIMIT", "auto-compact resolved")

                    if parser.is_approval(clean) and not self.awaiting_approval:
                        # Fix 2: 승인창 위 새 ⏺ 응답이 있으면 먼저 flush
                        await self._flush_completed(
                            app, chat_id, clean,
                            include_last=True, log_tag="pre-approval",
                        )

                        # P2-1: 승인 컨텍스트 저장
                        self.last_approval_summary = parser.summarize_approval(clean)
                        self.last_approval_context = self.last_approval_summary
                        self.last_approval_full = clean[-800:]
                        _log("AI-APPROVAL", self.last_approval_summary)

                        if self.status_msg_id:
                            try:
                                await app.bot.delete_message(chat_id, self.status_msg_id)
                            except Exception:
                                pass
                            self.status_msg_id = None
                        self.awaiting_approval = True
                        await sender._send_approval(app, chat_id, clean)
                        await asyncio.sleep(1)
                        continue

                    if self.awaiting_approval:
                        await asyncio.sleep(1)
                        continue

                    busy_now = parser.is_busy(clean)

                    # Fix 1: busy 진입 엣지에서 직전 ⏺ 응답 flush
                    if busy_now and not self.was_busy:
                        await self._flush_completed(
                            app, chat_id, clean,
                            include_last=True, log_tag="pre-busy",
                        )

                    # busy 진입
                    if busy_now and not self.was_busy:
                        self.was_busy = True
                        self.busy_started_at = time.time()
                        # P2: busy 진입 시 pane hash 추적 초기화
                        self.busy_pane_hash = hashlib.md5(clean.encode()).hexdigest()
                        self.busy_last_change_at = time.time()
                        self.saw_compaction = False
                        # 잔존 스크롤백의 Crunched/Compacting 마커로 오탐하지 않도록
                        # busy 진입 시점의 상태를 snapshot — 이후 "새로 등장한 경우" 에만 감지
                        self._pre_busy_had_compact = parser.has_compaction(clean)
                        # busy 스트리밍 타이머 초기화 — 진입 직후 1회는 즉시 가능
                        self.busy_last_stream_at = None
                        label, cc_sec = parser.busy_status(clean)
                        self.last_status_label = label
                        _log("AI-BUSY", label)
                        try:
                            init_text = parser._format_busy_status(label, cc_sec, 0)
                            msg = await app.bot.send_message(chat_id, init_text)
                            self.status_msg_id = msg.message_id
                        except Exception as e:
                            _log("STATUS-MSG-ERR", str(e))
                            self.status_msg_id = None

                    # busy 지속: 상태 메시지 편집 + watchdog
                    if busy_now:
                        # P1: 압축 이벤트 감지 (한 번만 로그)
                        if (
                            not self.saw_compaction
                            and parser.has_compaction(clean)
                            and not self._pre_busy_had_compact
                        ):
                            self.saw_compaction = True
                            _log("AI-COMPACT", "compaction detected during busy")

                        # P2: pane 내용 변화 추적
                        cur_hash = hashlib.md5(clean.encode()).hexdigest()
                        now_ts = time.time()
                        if cur_hash != self.busy_pane_hash:
                            self.busy_pane_hash = cur_hash
                            self.busy_last_change_at = now_ts

                        # P0/P2: watchdog 트리거 판정
                        timeout_trigger = (
                            self.busy_started_at
                            and now_ts - self.busy_started_at > BUSY_TIMEOUT_SEC
                        )
                        stuck_trigger = (
                            self.busy_last_change_at
                            and now_ts - self.busy_last_change_at > BUSY_STUCK_SEC
                        )
                        if timeout_trigger or stuck_trigger:
                            reason = (
                                f"작업 {int(now_ts - self.busy_started_at)}초 초과"
                                if timeout_trigger
                                else f"화면 {int(now_ts - self.busy_last_change_at)}초 멈춤"
                            )
                            await self._busy_watchdog(app, chat_id, clean, reason=reason)
                            pending = ""
                            await asyncio.sleep(1)
                            continue

                        # busy 중 ⏺ 블록 주기적 스트리밍 (bypass 모드 장시간 busy 대응).
                        # 마지막 블록은 아직 성장 중이므로 제외(include_last=False).
                        # queue.idx 로 이미 보낸 블록은 자동 제외된다.
                        should_stream = (
                            self.busy_last_stream_at is None
                            or now_ts - self.busy_last_stream_at >= BUSY_STREAM_SEC
                        )
                        if should_stream:
                            self.busy_last_stream_at = now_ts
                            await self._flush_completed(
                                app, chat_id, clean,
                                include_last=False, log_tag="busy-stream",
                            )

                        if self.status_msg_id and self.busy_started_at:
                            elapsed = int(now_ts - self.busy_started_at)
                            label, cc_sec = parser.busy_status(clean)
                            new_text = parser._format_busy_status(label, cc_sec, elapsed)
                            if new_text != getattr(self, "_last_status_text", ""):
                                try:
                                    await app.bot.edit_message_text(
                                        chat_id=chat_id,
                                        message_id=self.status_msg_id,
                                        text=new_text,
                                    )
                                    self._last_status_text = new_text
                                except Exception:
                                    pass
                        pending = ""
                        await asyncio.sleep(2)
                        continue

                    # busy 종료 직후: 상태 메시지 삭제
                    if self.was_busy and self.status_msg_id:
                        try:
                            await app.bot.delete_message(chat_id, self.status_msg_id)
                        except Exception:
                            pass
                        self.status_msg_id = None
                        self._last_status_text = ""

                    # P1: 압축을 거쳐 빠져나온 경우 — 응답 복구 or 재전송 안내
                    if self.was_busy and self.saw_compaction:
                        completed = self._completed_blocks(clean, include_last=True)
                        has_new = bool(self.queue.take_new(completed))
                        if has_new:
                            try:
                                await app.bot.send_message(
                                    chat_id, "ℹ️ 컨텍스트 압축 후 응답 복구"
                                )
                            except Exception:
                                pass
                            await self._flush_completed(
                                app, chat_id, clean,
                                include_last=True, log_tag="post-compact",
                            )
                        else:
                            # auto_compacting 상태면 이미 /compact dispatch 시점에 안내했으므로 중복 생략
                            if not self.auto_compacting:
                                note = "ℹ️ 컨텍스트 압축 발생 — 메시지를 다시 보내주세요."
                                try:
                                    await app.bot.send_message(chat_id, note)
                                except Exception:
                                    pass
                            _log("AI-COMPACT-NOSEND", "response not found post-compact")
                        self.saw_compaction = False
                        self._pre_busy_had_compact = False
                        self.auto_compacting = False
                        self.busy_pane_hash = ""
                        self.busy_last_change_at = None
                        # settle 로직이 동일 내용 재전송하지 않도록 hash/pending 동기화
                        self.last_hash = hashlib.md5(clean.encode()).hexdigest()
                        pending = ""
                        self.was_busy = False
                        await asyncio.sleep(1)
                        continue

                    # 대기 상태 → SETTLE_TICKS 안정 후 전송
                    h = hashlib.md5(clean.encode()).hexdigest()
                    if h != self.last_hash:
                        self.last_hash = h
                        pending = clean
                        settle = 0
                    else:
                        settle += 1

                    if pending and settle >= SETTLE_TICKS:
                        sent_count = await self._flush_completed(
                            app, chat_id, pending,
                            include_last=True, log_tag="response",
                        )
                        # 부팅 직후 ⏺ 블록이 전혀 없는 상태라면 (Welcome/ready 메시지)
                        # pane tail 을 first-boot fallback 으로 송출한다.
                        if not self.boot_notified and sent_count == 0:
                            blocks = parser.extract_response_blocks(pending)
                            if not blocks and not self.awaiting_approval:
                                fallback = "\n".join(pending.splitlines()[-30:]).strip()
                                if fallback:
                                    _log("AI→BOT", f"first boot ({len(fallback)} chars)")
                                    try:
                                        await sender._send_output(app, chat_id, fallback)
                                        _log("BOT→USER", "delivered (first-boot)")
                                        self.boot_notified = True
                                    except Exception as e:
                                        _log("FIRST-BOOT-SEND-FAIL", str(e))
                        pending = ""
                        self.was_busy = False

                    error_count = 0  # P1-6: 정상 동작 시 리셋

                except asyncio.CancelledError:
                    raise  # P1-5: CancelledError 반드시 전파
                except Exception as e:
                    error_count += 1
                    _log("MONITOR-ERROR", f"[{error_count}] {e}")
                    if error_count == 5:
                        try:
                            await app.bot.send_message(chat_id, f"⚠️ 모니터 오류 {error_count}회 연속 발생")
                        except Exception:
                            pass
                    elif error_count >= 10:
                        try:
                            await app.bot.send_message(chat_id, f"❌ 모니터 오류 {error_count}회 연속 — 모니터 종료")
                        except Exception:
                            pass
                        self.running = False
                        break

                await asyncio.sleep(1)

        except asyncio.CancelledError:  # P1-5: 외부 cancel 처리
            _log("MONITOR", "cancelled — cleanup")
            dump.event("core", "monitor_cancelled")
            dump.stop_tick()
            raise
        finally:
            # 정상 종료(running=False break) / cancel 경로 모두 커버.
            await self._pipe_detach()


# 싱글톤 — chat_id 1:1 가정.
bridge = Bridge()
