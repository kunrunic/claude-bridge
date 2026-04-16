"""Bridge 오케스트레이터 + monitor 루프.

Telegram ↔ Claude(tmux) 중계의 중심. receiver 에서 호출하는 start/stop 과
자체 수행하는 monitor() 루프로 이루어진다.

sibling 모듈 호출은 `<module>.<name>` 형태로 접근해 테스트 패치 지점을
단일 위치 (`bridge.<module>.<name>`) 로 고정한다.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from pathlib import Path

from telegram.ext import Application

from . import config, parser, sender, session, tmux
from .config import (
    BUSY_STREAM_SEC,
    BUSY_STUCK_SEC,
    BUSY_TIMEOUT_SEC,
    CLAUDE,
    DEDUP_TTL_SEC,
    MAX_SENT_HISTORY,
    SETTLE_TICKS,
    STOP_WAIT_SEC,
    _log,
)


class Bridge:
    def __init__(self):
        self.chat_id: int | None = None
        self.running: bool = False
        self.task: asyncio.Task | None = None
        self.last_hash: str = ""
        self.last_sent: str = ""
        self.awaiting_approval: bool = False
        self.skip_permissions: bool = False
        self.current_session_id: str | None = None
        self.last_approval_summary: str = ""
        self.last_approval_context: str = ""   # P2-1: 도구명 + 요약
        self.last_approval_full: str = ""      # P2-1: pane 전체 내용 (재연결 시 전달용)
        # (응답 키, 전송 시각) — 중복 방지.
        # TODO: 멀티 chat_id 전환 시 Bridge 인스턴스 분리 필요
        self._sent_keys: list[tuple[str, float]] = []
        # busy 세션 내 이미 스트리밍한 완료 블록 수 (position-based dedup)
        self.busy_stream_idx: int = 0
        self.limit_reported: bool = False      # 한도 초과 알림 중복 방지
        self._state_lock = asyncio.Lock()      # P1-4: 상태 직렬화

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
        tmux.tmux_run(["set-option", "-t", config.TMUX, "remain-on-exit", "on"])
        return True

    def is_alive(self) -> bool:
        """tmux 세션에 실행 중인 프로세스가 살아있는지."""
        r = tmux.tmux_run(["list-panes", "-t", config.TMUX, "-F", "#{pane_dead}"])
        if r.returncode != 0:
            return False
        return r.stdout.strip() == "0"

    async def is_alive_async(self) -> bool:
        r = await tmux.tmux_run_async(["list-panes", "-t", config.TMUX, "-F", "#{pane_dead}"])
        if r.returncode != 0:
            return False
        return r.stdout.strip() == "0"

    def start(self, session_id: str | None = None, chat_id: int = 0) -> bool:
        if session_id and not session._acquire_lock(session_id, chat_id):
            return False
        session._release_lock(self.current_session_id)
        self.current_session_id = session_id
        tmux.tmux_run(["kill-session", "-t", config.TMUX])
        return self._spawn(session_id)

    def restart(self) -> bool:
        self.running = False
        if self.task:
            self.task.cancel()
        tmux.tmux_run(["kill-session", "-t", config.TMUX])
        return self._spawn(self.current_session_id)

    def perm_label(self) -> str:
        if self.skip_permissions:
            return "[권한 스킵 ON]  탭하면 OFF"
        return "[권한 확인 ON]  탭하면 스킵"

    # -- dedup 헬퍼 --------------------------------------------------------

    def _response_key(self, text: str) -> str:
        """중복 판정용 정규화 키 — 빈 줄/공백 무시한 정규화 문자열."""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines)

    def _prune_sent(self):
        """DEDUP_TTL_SEC 이상 지난 항목 제거."""
        cutoff = time.time() - DEDUP_TTL_SEC
        self._sent_keys = [(k, t) for k, t in self._sent_keys if t >= cutoff]

    def _already_sent(self, text: str) -> bool:
        """최근 DEDUP_TTL_SEC 내 전송 내역과 비교해 중복인지 확인."""
        self._prune_sent()
        key = self._response_key(text)
        return any(k == key for k, _ in self._sent_keys)

    def _mark_sent(self, text: str):
        self._prune_sent()
        self._sent_keys.append((self._response_key(text), time.time()))
        if len(self._sent_keys) > MAX_SENT_HISTORY:
            self._sent_keys.pop(0)

    def _commit_sent(self, text: str):
        """전송 확정 — last_sent 갱신과 dedup 기록을 원자적으로 묶는다.
        scope: 현 Bridge 인스턴스 (chat_id 1:1 가정).
        새 전송 경로 추가 시 반드시 이 헬퍼를 사용할 것."""
        self.last_sent = text
        self._mark_sent(text)

    # -- 종료 --------------------------------------------------------------

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
        tmux.send_input("/exit")
        await asyncio.sleep(STOP_WAIT_SEC)
        tmux.tmux_run(["kill-session", "-t", config.TMUX])
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
        pane 에서 마지막 ⏺ 응답을 추출해 복구 전송하고, 없으면 재전송 안내.
        어떤 경우든 busy state 를 강제로 리셋한다.
        """
        _log("AI-WATCHDOG", f"trigger={reason}")

        if self.status_msg_id:
            try:
                await app.bot.delete_message(chat_id, self.status_msg_id)
            except Exception:
                pass
            self.status_msg_id = None
            self._last_status_text = ""

        response = parser.extract_last_response(clean)
        has_new = (
            response
            and "⏺" in response
            and response != self.last_sent
            and not self._already_sent(response)
        )

        if has_new:
            try:
                await app.bot.send_message(chat_id, f"⚠️ {reason} — 직전 응답 복구")
            except Exception:
                pass
            _log("AI→BOT", f"watchdog recovery ({len(response)} chars)")
            try:
                await sender._send_output(app, chat_id, response)
                _log("BOT→USER", "delivered (watchdog)")
            except Exception as e:
                _log("WATCHDOG-SEND-FAIL", str(e))
            self._commit_sent(response)
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
        self.last_hash = self.last_sent = ""
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
        self.busy_stream_idx: int = 0                   # busy 세션 내 이미 스트리밍한 완료 블록 수
        settle = 0
        pending = ""
        error_count = 0   # P1-6: 연속 오류 카운터

        # 부팅 시점에 pane 에 이미 있던 ⏺ 응답은 "이미 사용자가 본 것" 으로 간주.
        # last_sent/_sent_keys 에는 기록하지 않고 last_hash 만 고정 — 이렇게 하면
        # pane 내용이 변하지 않는 한 settle 파이프라인에 진입하지 않아 재전송되지 않고,
        # 이후 Claude 가 우연히 seed 와 동일한 문자열로 응답해도 dedup 에 걸리지 않는다.
        try:
            seed_clean = parser.strip_ansi(await tmux.pane_output_async()).strip()
            seed_resp = parser.extract_last_response(seed_clean)
            if seed_resp:
                self.last_hash = hashlib.md5(seed_clean.encode()).hexdigest()
                _log("BOOT-SEED", f"마지막 ⏺ 블록 {len(seed_resp)}자 감지 — pane hash 고정")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log("BOOT-SEED-ERROR", str(e))

        try:
            while self.running:
                try:
                    check = await tmux.tmux_run_async(["has-session", "-t", config.TMUX])
                    if check.returncode != 0:
                        if not self.dead_reported:
                            await app.bot.send_message(chat_id, "tmux 세션이 사라졌습니다. /start로 다시 시작하세요.")
                            self.dead_reported = True
                            self.running = False
                        break

                    if not await self.is_alive_async():
                        if not self.dead_reported:
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
                    if parser.is_trust_prompt(clean):
                        if not self.last_sent.endswith("__trust_ack__"):
                            tmux.send_key("Enter")
                            _log("AUTO-ACK", "trust prompt")
                            await app.bot.send_message(chat_id, "폴더 신뢰 프롬프트 자동 승인")
                            self.last_sent = clean + "__trust_ack__"
                        await asyncio.sleep(1)
                        continue

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
                        # Fix 2: 승인창 위 새 ⏺ 응답이 있으면 먼저 전송
                        response = parser.extract_last_response(clean)
                        if (response and "⏺" in response
                                and response != self.last_sent
                                and not self._already_sent(response)):
                            _log("AI→BOT", f"pre-approval flush ({len(response)} chars)")
                            await sender._send_output(app, chat_id, response)
                            _log("BOT→USER", "delivered (pre-approval flush)")
                            self._commit_sent(response)

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
                        response = parser.extract_last_response(clean)
                        if (response and "⏺" in response
                                and response != self.last_sent
                                and not self._already_sent(response)):
                            _log("AI→BOT", f"pre-busy flush ({len(response)} chars)")
                            await sender._send_output(app, chat_id, response)
                            _log("BOT→USER", "delivered (pre-busy flush)")
                            self._commit_sent(response)

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
                        # busy 스트리밍 타이머/인덱스 초기화 — 진입 직후 1회는 즉시 가능
                        self.busy_last_stream_at = None
                        self.busy_stream_idx = 0
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

                        # busy 중 ⏺ 블록 주기적 스트리밍 (bypass 모드 장시간 busy 대응)
                        # "완료된" 블록(=뒤에 다음 ⏺ 이 나타나 더 이상 성장하지 않는 블록) 만
                        # 전송. 마지막 블록은 아직 성장 중이므로 skip → busy 종료 시 SETTLE 에서 처리.
                        should_stream = (
                            self.busy_last_stream_at is None
                            or now_ts - self.busy_last_stream_at >= BUSY_STREAM_SEC
                        )
                        if should_stream and not self.awaiting_approval:
                            self.busy_last_stream_at = now_ts
                            blocks = parser.extract_response_blocks(clean)
                            # 마지막 블록 제외 (성장 중) + 실행 중 도구 블록 제외
                            completed = [b for b in blocks[:-1] if not parser._is_block_active(b)]
                            # position-based: busy 세션 내 이미 전송한 완료 블록 개수를 넘어선 것만 송신.
                            # content 가 바뀌어도(진행 카운터, todo 체크 등) 같은 인덱스면 재전송 안 함.
                            new_blocks = completed[self.busy_stream_idx:]
                            for blk in new_blocks:
                                # 방어선: 스크롤백 밀림/재정렬 대비 content-hash 1회 더 체크
                                if self._already_sent(blk):
                                    continue
                                _log("AI→BOT", f"stream block ({len(blk)} chars)")
                                try:
                                    await sender._send_output(app, chat_id, blk)
                                    _log("BOT→USER", "delivered (busy-stream)")
                                    self._commit_sent(blk)
                                except Exception as e:
                                    _log("BUSY-STREAM-FAIL", str(e))
                                    break
                            # idx 는 단조증가 — 스크롤백으로 completed 길이가 줄어도 retreat 금지
                            self.busy_stream_idx = max(self.busy_stream_idx, len(completed))

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
                        response = parser.extract_last_response(clean)
                        has_new = (
                            response
                            and "⏺" in response
                            and response != self.last_sent
                            and not self._already_sent(response)
                        )
                        if has_new:
                            try:
                                await app.bot.send_message(
                                    chat_id, "ℹ️ 컨텍스트 압축 후 응답 복구"
                                )
                            except Exception:
                                pass
                            _log("AI→BOT", f"post-compact ({len(response)} chars)")
                            try:
                                await sender._send_output(app, chat_id, response)
                                _log("BOT→USER", "delivered (post-compact)")
                            except Exception as e:
                                _log("POST-COMPACT-SEND-FAIL", str(e))
                            self._commit_sent(response)
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

                    if pending and settle >= SETTLE_TICKS and pending != self.last_sent:
                        response = parser.extract_last_response(pending)
                        is_first = not self.last_sent

                        to_send = None
                        if response and "⏺" in response:
                            if response != self.last_sent and not self._already_sent(response):
                                to_send = response
                        elif is_first:
                            to_send = "\n".join(pending.splitlines()[-30:]).strip()

                        if to_send and not self.awaiting_approval:
                            _log("AI→BOT", f"response ({len(to_send)} chars)")
                            await sender._send_output(app, chat_id, to_send)
                            _log("BOT→USER", "delivered")
                            self._commit_sent(to_send)
                        else:
                            if response and "⏺" in response and self._already_sent(response):
                                _log("AI-DROP-DUP", f"dedup suppressed ({len(response)} chars)")
                            self.last_sent = response or pending
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
            raise


# 싱글톤 — chat_id 1:1 가정.
bridge = Bridge()
