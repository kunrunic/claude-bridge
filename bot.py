#!/usr/bin/env python3
"""
claude-bridge: Telegram <-> Claude Code tmux 브리지 데몬
/start -> 세션 목록 -> 선택 -> tmux에서 Claude 실행 -> 양방향 중계
/end   -> 세션 종료
"""

import asyncio
import subprocess
import json
import re
import hashlib
import sys
import time
from pathlib import Path
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)
from telegram.request import HTTPXRequest

# -- 상수 -----------------------------------------------------------------------

TMUX_SCROLL_LINES   = 200   # pane 캡처 줄 수
APPROVAL_SCAN_LINES = 30    # 승인 박스 스캔 범위
SETTLE_TICKS        = 2     # 화면 안정화 틱 수
MAX_SENT_HISTORY    = 20    # 중복 방지 히스토리 최대 개수
STOP_WAIT_SEC       = 5     # stop() /exit 후 대기 초
MAX_MSG_CHARS       = 3500  # Telegram 메시지 최대 길이
BUSY_CHECK_TAIL     = 20    # busy 감지 꼬리 줄 수
BUSY_TIMEOUT_SEC    = 600   # busy 최대 지속 시간 — 초과 시 watchdog 발동 (P0)
BUSY_STUCK_SEC      = 300   # pane 내용 무변화 지속 시 stuck 판정 (P2)
COMPACT_SCAN_LINES  = 8     # 압축 이벤트 감지 스캔 범위 (현재 활성 영역만)

# -- 설정 -----------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent / "config.json"

def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        sys.exit(f"[FATAL] config.json 없음: {_CONFIG_PATH}")
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"[FATAL] config.json 파싱 오류: {e}")
    for key in ("token", "claude_path", "allowed_ids"):
        if key not in cfg:
            sys.exit(f"[FATAL] config.json 필수 항목 누락: {key}")
    return cfg

_cfg = _load_config()

TOKEN       = _cfg["token"]
TMUX        = _cfg.get("tmux_session", "claude_bridge")
CLAUDE      = _cfg["claude_path"]
PROJECTS    = Path.home() / ".claude" / "projects"
ALLOWED_IDS: set[int] = set(_cfg.get("allowed_ids", []))

def _log(tag: str, msg: str = ""):
    """구조화된 포그라운드 로그"""
    ts = time.strftime("%H:%M:%S")
    if msg:
        print(f"[{ts}] [{tag}] {msg}")
    else:
        print(f"[{ts}] [{tag}]")

def is_allowed(update) -> bool:
    return update.effective_chat.id in ALLOWED_IDS

async def deny(update: Update):
    await update.effective_message.reply_text(
        f"접근 거부. 이 봇은 등록된 사용자만 사용할 수 있습니다.\n"
        f"본인 확인용 ID: {update.effective_chat.id}"
    )

# Claude Code 승인 프롬프트 감지 패턴
# "Do you want to proceed" 가 정식 승인창 표식. ❯ 1. Yes 단독은 신뢰 프롬프트와 충돌하므로 사용 X
APPROVAL_RE = re.compile(
    r"Do you want to proceed|"
    r"Allow\s+\w+\s+to|Proceed\?|\(Y/n\)|\(y/N\)"
)
# 폴더 신뢰 프롬프트 (새 디렉토리 진입 시 한 번 뜸)
TRUST_RE    = re.compile(
    r"Quick safety check|"
    r"Is this a project you created|"
    r"trust this folder|"
    r"Yes, I trust|"
    r"No, exit|"
    r"Security guide"
)
# Claude가 "작업 중" 상태 - "esc to interrupt"만 신뢰 가능한 활성 신호
# (Running/Compacting 등은 과거 로그에도 남아서 오탐 발생)
BUSY_RE     = re.compile(r"esc to interrupt")
# 사용량 한도 초과 감지
LIMIT_RE    = re.compile(r"You've hit your limit|hit your (daily )?limit", re.IGNORECASE)
# 컨텍스트 압축 이벤트 (자동 /compact 또는 'Crunched for N' 요약 라인)
COMPACT_RE  = re.compile(r"Compacting conversation|Crunched\s+for\s+\d+", re.IGNORECASE)
# Context limit 도달 (CB2 케이스 — 사용자 메시지가 처리되지 못하고 압축이 강제됨)
CONTEXT_LIMIT_RE = re.compile(r"Context limit reached", re.IGNORECASE)
# /compact가 API 에러로 실패한 경우 (1M 컨텍스트 + Extra Usage 미활성 등)
COMPACT_ERROR_RE = re.compile(r"Error during compaction", re.IGNORECASE)
ANSI_RE     = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# -- tmux 유틸 -----------------------------------------------------------------

def tmux_run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["/opt/homebrew/bin/tmux"] + cmd,
            capture_output=True, text=True, timeout=5.0,
        )
    except subprocess.TimeoutExpired:
        _log("TMUX-TIMEOUT", f"cmd={cmd[:3]}")
        r = subprocess.CompletedProcess(cmd, returncode=1)
        r.stdout = ""
        r.stderr = "timeout"
        return r

async def tmux_run_async(cmd: list[str]) -> subprocess.CompletedProcess:
    """asyncio 이벤트 루프를 블로킹하지 않는 tmux_run"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, tmux_run, cmd)

def pane_output() -> str:
    """현재 tmux 패널 내용 반환 (마지막 TMUX_SCROLL_LINES줄)"""
    return tmux_run(
        ["capture-pane", "-t", TMUX, "-p", "-S", f"-{TMUX_SCROLL_LINES}"]
    ).stdout

async def pane_output_async() -> str:
    """pane_output의 비동기 버전"""
    r = await tmux_run_async(
        ["capture-pane", "-t", TMUX, "-p", "-S", f"-{TMUX_SCROLL_LINES}"]
    )
    return r.stdout

def send_input(text: str):
    """Claude에 텍스트 입력 후 Enter (literal 모드로 안전하게)"""
    tmux_run(["send-keys", "-t", TMUX, "-l", text])
    time.sleep(0.1)
    tmux_run(["send-keys", "-t", TMUX, "Enter"])

def send_key(key: str):
    """Enter / Down / Escape 등 특수 키 전송"""
    tmux_run(["send-keys", "-t", TMUX, key])

def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)

def is_trust_prompt(text: str) -> bool:
    return bool(TRUST_RE.search(text))

def is_approval(text: str) -> bool:
    return bool(APPROVAL_RE.search(text))

def summarize_approval(text: str) -> str:
    """승인 요청 박스에서 도구명만 추출 (상세 내용은 이미 ⏺ Bash(...) 로 전달됨)"""
    raw_lines = text.splitlines()

    proceed_idx = -1
    for i in range(len(raw_lines) - 1, -1, -1):
        if "Do you want to" in raw_lines[i]:
            proceed_idx = i
            break
    if proceed_idx < 0:
        return "승인"

    start = max(0, proceed_idx - APPROVAL_SCAN_LINES)
    for ln in raw_lines[start:proceed_idx]:
        s = ln.strip()
        m = re.match(r"^(Bash|Edit|Write|Read|MultiEdit|WebFetch|Grep|Glob|Task)\b", s)
        if m:
            return m.group(1)
    return "승인"

def is_busy(text: str) -> bool:
    """Claude가 처리 중인지 — 현재 상태바 영역(마지막 divider 이하)만 검사.
    scrollback에 남은 과거 상태바의 'esc to interrupt'로 인한 오탐을 막는다.
    """
    lines = text.splitlines()
    # 마지막 ~10줄 안에서 divider 찾기 (상태바 구조: divider/❯/divider/status)
    search_window = max(0, len(lines) - 10)
    for i in range(len(lines) - 1, search_window - 1, -1):
        s = lines[i].strip()
        if s and len(s) > 20 and all(c in "─" for c in s):
            tail = "\n".join(lines[i:])
            return bool(BUSY_RE.search(tail))
    # divider 못 찾으면 마지막 BUSY_CHECK_TAIL줄로 fallback
    tail = "\n".join(lines[-BUSY_CHECK_TAIL:])
    return bool(BUSY_RE.search(tail))

def has_compaction(text: str) -> bool:
    """pane tail에 컨텍스트 압축 이벤트 흔적이 있는지 — P1"""
    tail = "\n".join(text.splitlines()[-COMPACT_SCAN_LINES:])
    return bool(COMPACT_RE.search(tail))

def has_context_limit(text: str) -> bool:
    """'Context limit reached' 표식이 tail에 있는지 — CB2 케이스 식별용"""
    tail = "\n".join(text.splitlines()[-COMPACT_SCAN_LINES:])
    return bool(CONTEXT_LIMIT_RE.search(tail))

def has_compaction_error(text: str) -> bool:
    """'Error during compaction' — /compact가 API 단에서 거절된 경우"""
    tail = "\n".join(text.splitlines()[-COMPACT_SCAN_LINES:])
    return bool(COMPACT_ERROR_RE.search(tail))

_STATUS_LINE_RE = re.compile(
    r"(?:[·✻⋯*]\s*)?"
    r"(Compacting[^\n│]*|Thinking[^\n│]*|Cerebrating[^\n│]*|Pondering[^\n│]*|"
    r"Cogitat\w+[^\n│]*|Crunching[^\n│]*|Running[^\n│]*|Tool use[^\n│]*)",
    re.IGNORECASE,
)

_CC_TIMER_RE = re.compile(r"\(\s*(\d+)\s*s\b[^)]*\)?")


def _format_busy_status(label: str, cc_sec: str | None, bot_elapsed: int) -> str:
    """상태 메시지 포맷.

    Claude 타이머가 있으면:   ⏳ Compacting conversation · 🧠 26s · 🤖 28s
    없으면:                    ⏳ Thinking · 🤖 12s
    """
    base = f"⏳ {label}"
    if cc_sec:
        return f"{base} · 🧠 {cc_sec} · 🤖 {bot_elapsed}s"
    return f"{base} · 🤖 {bot_elapsed}s"


def busy_status(text: str) -> tuple[str, str | None]:
    """화면에서 진행 상태 라인 추출 → (액션, Claude 내장 초).

    예: "✻ Compacting conversation… (26s · esc to interrupt)"
        → ("Compacting conversation…", "26s")
    Claude 타이머가 없으면 두 번째 값은 None.
    """
    tail_lines = text.splitlines()[-25:]
    for line in reversed(tail_lines):
        m = _STATUS_LINE_RE.search(line)
        if m:
            label = m.group(1).strip()
            for cut in ("esc to interrupt", "ctrl+"):
                idx = label.lower().find(cut)
                if idx > 0:
                    label = label[:idx].strip()
            # Claude 타이머 `(Ns ...)` 분리
            cc_sec: str | None = None
            tm = _CC_TIMER_RE.search(label)
            if tm:
                cc_sec = f"{tm.group(1)}s"
                label = _CC_TIMER_RE.sub("", label).strip(" ·-—|")
            # 꼬리 장식 문자 정리
            label = label.rstrip(" ·…").strip()
            if not label:
                label = "작업 중"
            return label[:80], cc_sec
    return "작업 중", None

def extract_last_response(text: str) -> str:
    """화면 출력에서 Claude의 마지막 응답만 추출"""
    lines = text.splitlines()

    def is_divider(line: str) -> bool:
        s = line.strip()
        return bool(s) and len(s) > 20 and all(c in "─" for c in s)

    end = len(lines)

    # 1) 승인 박스/입력창 경계 감지
    prompt_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if "Do you want to proceed" in line or line == "Do you want to":
            prompt_idx = i
            break
    if prompt_idx > 0:
        for i in range(prompt_idx - 1, max(-1, prompt_idx - 60), -1):
            if is_divider(lines[i]):
                end = min(end, i)
                break

    # 2) 입력창 divider (마지막 ~15줄)
    divider_positions = []
    for i in range(len(lines) - 1, max(-1, len(lines) - 15), -1):
        if is_divider(lines[i]):
            divider_positions.append(i)
        if "Welcome back" in lines[i] or "Claude Code v" in lines[i]:
            end = min(end, i)
    if divider_positions:
        end = min(end, min(divider_positions))

    # 마지막 ⏺ (Claude 응답 시작) 찾기
    start = -1
    for i in range(end - 1, -1, -1):
        if lines[i].lstrip().startswith("⏺"):
            start = i
            break

    if start < 0:
        return ""

    # ⏺ 이후에 새 ❯ 사용자 입력이 있으면 그 전까지만
    for i in range(start + 1, end):
        if lines[i].lstrip().startswith("❯ "):
            end = i
            break

    return "\n".join(lines[start:end]).strip()

# -- 세션 락 (멀티 인스턴스 동시 resume 방지) ----------------------------------

_LOCK_DIR = Path.home() / ".claude"

def _lock_path(session_id: str) -> Path:
    return _LOCK_DIR / f".cb_lock_{session_id}"

def _parse_lock(session_id: str) -> tuple[str, int] | None:
    """락파일에서 (tmux_name, chat_id) 반환. 없거나 파싱 실패 시 None"""
    lp = _lock_path(session_id)
    try:
        lines = lp.read_text().splitlines()
        return lines[0].strip(), int(lines[1].strip())
    except Exception:
        return None

def _acquire_lock(session_id: str, chat_id: int) -> bool:
    """락 획득. 이미 다른 인스턴스가 점유 중이면 False"""
    lp = _lock_path(session_id)
    try:
        fd = lp.open("x")
        fd.write(f"{TMUX}\n{chat_id}")
        fd.close()
        return True
    except FileExistsError:
        info = _parse_lock(session_id)
        if info:
            owner_tmux, _ = info
            if tmux_run(["has-session", "-t", owner_tmux]).returncode != 0:
                lp.unlink(missing_ok=True)
                return _acquire_lock(session_id, chat_id)
        return False
    except Exception as e:
        _log("LOCK-ERROR", str(e))
        return True  # 락 디렉토리 문제 시 허용 (방어적)

def _release_lock(session_id: str | None):
    """내 TMUX 세션이 소유한 락만 삭제"""
    if not session_id:
        return
    lp = _lock_path(session_id)
    try:
        info = _parse_lock(session_id)
        if info and info[0] == TMUX:
            lp.unlink(missing_ok=True)
    except Exception as e:
        _log("LOCK-RELEASE-ERROR", str(e))

def _is_locked(session_id: str) -> bool:
    lp = _lock_path(session_id)
    if not lp.exists():
        return False
    info = _parse_lock(session_id)
    if not info:
        lp.unlink(missing_ok=True)
        return False
    owner_tmux, _ = info
    if tmux_run(["has-session", "-t", owner_tmux]).returncode != 0:
        lp.unlink(missing_ok=True)
        return False
    return True

def _my_locks() -> list[tuple[str, int]]:
    """이 인스턴스(TMUX)가 소유한 락 목록 → [(session_id, chat_id), ...]"""
    result = []
    try:
        for lp in _LOCK_DIR.glob(".cb_lock_*"):
            session_id = lp.name[len(".cb_lock_"):]
            info = _parse_lock(session_id)
            if info and info[0] == TMUX:
                result.append((session_id, info[1]))
    except Exception as e:
        _log("MY-LOCKS-ERROR", str(e))
    return result

# -- 세션 탐색 -----------------------------------------------------------------

def find_sessions(limit: int = 8) -> list[dict]:
    now = time.time()
    candidates = []
    for p in PROJECTS.rglob("*.jsonl"):
        if p.stem.startswith("agent-"):
            continue
        title, last, last_ts = _parse_session_msgs(p)
        if not title:
            continue
        activity_ts = last_ts if last_ts > 0 else p.stat().st_mtime
        if _is_locked(p.stem):
            continue
        proj_slug = p.parent.name.lstrip("-")
        proj_short = proj_slug.split("-")[-1] if proj_slug else ""
        candidates.append({
            "id":          p.stem,
            "project":     proj_short,
            "activity_ts": activity_ts,
            "mtime":       datetime.fromtimestamp(activity_ts).strftime("%m/%d %H:%M"),
            "title":       title[:40],
            "last":        last[:40],
        })

    candidates.sort(key=lambda x: x["activity_ts"], reverse=True)
    return candidates[:limit]

def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return item["text"]
    return ""

def _parse_session_msgs(path: Path) -> tuple[str, str, float]:
    """(첫 메시지, 마지막 메시지, 마지막 활동 timestamp) 반환"""
    first = ""
    last  = ""
    last_ts = 0.0
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    ts_str = d.get("timestamp", "")
                    if ts_str:
                        try:
                            from datetime import datetime as _dt
                            ts = _dt.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                            if ts > last_ts:
                                last_ts = ts
                        except Exception:
                            pass

                    if d.get("type") != "user":
                        continue
                    text = _extract_text(d.get("message", {}).get("content", ""))
                    if not text or text.startswith("<") or len(text) < 15:
                        continue
                    stripped = text.strip()
                    if stripped.startswith("/") and " " not in stripped:
                        continue
                    if not first:
                        first = stripped
                    last = stripped
                except Exception:
                    pass
    except Exception:
        pass
    return first, last, last_ts

def get_session_cwd(session_id: str) -> str | None:
    """JSONL에서 세션의 cwd 추출"""
    for p in PROJECTS.rglob(f"{session_id}.jsonl"):
        try:
            with open(p, errors="ignore") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        if "cwd" in d:
                            return d["cwd"]
                    except Exception:
                        pass
        except Exception:
            pass
    return None

# -- 브리지 상태 ---------------------------------------------------------------

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
        self._sent_keys: list[str] = []        # 최근 전송 응답 키 (중복 방지)
        self.limit_reported: bool = False      # 한도 초과 알림 중복 방지
        self._state_lock = asyncio.Lock()      # P1-4: 상태 직렬화

    def _build_cmd(self, session_id: str | None) -> str:
        parts = [CLAUDE]
        if session_id:
            parts += ["--resume", session_id]
        if self.skip_permissions:
            parts.append("--dangerously-skip-permissions")
        return " ".join(parts)

    def _spawn(self, session_id: str | None) -> bool:
        """tmux 새 세션에 Claude 실행 + remain-on-exit 옵션"""
        claude_cmd = self._build_cmd(session_id)

        default_cwd = str(Path(__file__).parent)
        cwd = get_session_cwd(session_id) if session_id else default_cwd
        if not cwd:
            cwd = default_cwd
        wrapped = f"cd {cwd!r} && {claude_cmd}"

        r = tmux_run([
            "new-session", "-d", "-s", TMUX,
            "-x", "80", "-y", "50",
            "sh", "-c", wrapped,
        ])
        if r.returncode != 0:
            return False
        tmux_run(["set-option", "-t", TMUX, "remain-on-exit", "on"])
        return True

    def is_alive(self) -> bool:
        """tmux 세션에 실행 중인 프로세스가 살아있는지"""
        r = tmux_run(["list-panes", "-t", TMUX, "-F", "#{pane_dead}"])
        if r.returncode != 0:
            return False
        return r.stdout.strip() == "0"

    async def is_alive_async(self) -> bool:
        r = await tmux_run_async(["list-panes", "-t", TMUX, "-F", "#{pane_dead}"])
        if r.returncode != 0:
            return False
        return r.stdout.strip() == "0"

    def start(self, session_id: str | None = None, chat_id: int = 0) -> bool:
        if session_id and not _acquire_lock(session_id, chat_id):
            return False
        _release_lock(self.current_session_id)
        self.current_session_id = session_id
        tmux_run(["kill-session", "-t", TMUX])
        return self._spawn(session_id)

    def restart(self) -> bool:
        self.running = False
        if self.task:
            self.task.cancel()
        tmux_run(["kill-session", "-t", TMUX])
        return self._spawn(self.current_session_id)

    def perm_label(self) -> str:
        if self.skip_permissions:
            return "[권한 스킵 ON]  탭하면 OFF"
        return "[권한 확인 ON]  탭하면 스킵"

    def _response_key(self, text: str) -> str:
        """중복 판정용 정규화 키 — 빈 줄/공백 무시한 정규화 문자열"""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines)

    def _already_sent(self, text: str) -> bool:
        """최근 전송 내역(최대 MAX_SENT_HISTORY개)과 비교해 중복인지 확인"""
        key = self._response_key(text)
        return key in self._sent_keys

    def _mark_sent(self, text: str):
        key = self._response_key(text)
        self._sent_keys.append(key)
        if len(self._sent_keys) > MAX_SENT_HISTORY:
            self._sent_keys.pop(0)

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
        send_input("/exit")
        await asyncio.sleep(STOP_WAIT_SEC)
        tmux_run(["kill-session", "-t", TMUX])
        for sid, _ in _my_locks():
            _release_lock(sid)
        self.current_session_id = None

    async def _busy_watchdog(
        self,
        app: Application,
        chat_id: int,
        clean: str,
        *,
        reason: str,
    ) -> None:
        """busy 상태가 비정상적으로 오래 지속되거나 화면이 얼어붙었을 때 호출.
        pane에서 마지막 ⏺ 응답을 추출해 복구 전송하고, 없으면 재전송 안내.
        어떤 경우든 busy state를 강제로 리셋한다.
        """
        _log("AI-WATCHDOG", f"trigger={reason}")

        if self.status_msg_id:
            try:
                await app.bot.delete_message(chat_id, self.status_msg_id)
            except Exception:
                pass
            self.status_msg_id = None
            self._last_status_text = ""

        response = extract_last_response(clean)
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
                await _send_output(app, chat_id, response)
                _log("BOT→USER", "delivered (watchdog)")
            except Exception as e:
                _log("WATCHDOG-SEND-FAIL", str(e))
            self.last_sent = response
            self._mark_sent(response)
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
        settle = 0
        pending = ""
        error_count = 0   # P1-6: 연속 오류 카운터

        # 부팅 시점에 pane에 이미 있던 ⏺ 응답은 "이미 사용자가 본 것"으로 간주
        try:
            seed_clean = strip_ansi(await pane_output_async()).strip()
            seed_resp  = extract_last_response(seed_clean)
            if seed_resp:
                self.last_sent = seed_resp
                self._mark_sent(seed_resp)
                _log("BOOT-SEED", f"마지막 ⏺ 블록 {len(seed_resp)}자 무시 처리")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log("BOOT-SEED-ERROR", str(e))

        try:
            while self.running:
                try:
                    check = await tmux_run_async(["has-session", "-t", TMUX])
                    if check.returncode != 0:
                        if not self.dead_reported:
                            await app.bot.send_message(chat_id, "tmux 세션이 사라졌습니다. /start로 다시 시작하세요.")
                            self.dead_reported = True
                            self.running = False
                        break

                    if not await self.is_alive_async():
                        if not self.dead_reported:
                            out = await pane_output_async()
                            clean = strip_ansi(out).strip()
                            await app.bot.send_message(
                                chat_id,
                                f"Claude 프로세스가 종료되었습니다.\n마지막 출력:\n```\n{clean[-1500:]}\n```",
                                parse_mode="Markdown"
                            )
                            self.dead_reported = True
                            self.running = False
                        break

                    out = await pane_output_async()
                    clean = strip_ansi(out).strip()

                    # 승인/신뢰 프롬프트는 busy보다 우선 체크
                    if is_trust_prompt(clean):
                        if not self.last_sent.endswith("__trust_ack__"):
                            send_key("Enter")
                            _log("AUTO-ACK", "trust prompt")
                            await app.bot.send_message(chat_id, "폴더 신뢰 프롬프트 자동 승인")
                            self.last_sent = clean + "__trust_ack__"
                        await asyncio.sleep(1)
                        continue

                    # 사용량 한도 초과 감지
                    if LIMIT_RE.search(clean):
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

                    if self.limit_reported and not LIMIT_RE.search(clean):
                        self.limit_reported = False

                    # /compact 실패 감지 — API 에러 (1M 컨텍스트 Extra Usage 미활성 등).
                    # 자동 재dispatch 루프를 막고 사용자가 /model로 전환하도록 버튼 안내.
                    if has_compaction_error(clean):
                        if not self.compact_error_halted:
                            self.compact_error_halted = True
                            self.auto_compacting = False
                            _log("AI-COMPACT-ERR", "halt auto-compact; prompt model switch")
                            try:
                                await _send_model_switch_prompt(app, chat_id)
                            except Exception as e:
                                _log("MODEL-SWITCH-SEND-FAIL", str(e))
                        await asyncio.sleep(2)
                        continue

                    # 에러가 pane에서 사라지고 limit도 해제됐으면 halted 플래그 복구
                    if (self.compact_error_halted
                            and not has_compaction_error(clean)
                            and not has_context_limit(clean)):
                        self.compact_error_halted = False
                        _log("AI-COMPACT-ERR", "resolved (limit cleared)")

                    # Context limit 자동 대응 — Claude Code가 입력을 거부하므로
                    # 봇이 /compact를 대신 dispatch해서 흐름을 풀어준다.
                    # (compact_error_halted 중에는 재시도하지 않음)
                    if has_context_limit(clean) and not self.compact_error_halted:
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
                                send_input("/compact")
                            except Exception as e:
                                _log("CTX-LIMIT-SEND-FAIL", str(e))
                        await asyncio.sleep(2)
                        continue

                    # 자동 압축 해제: limit 흔적이 사라지고 busy도 아닐 때
                    if self.auto_compacting and not is_busy(clean):
                        self.auto_compacting = False
                        _log("AI-CTX-LIMIT", "auto-compact resolved")

                    if is_approval(clean) and not self.awaiting_approval:
                        # Fix 2: 승인창 위 새 ⏺ 응답이 있으면 먼저 전송
                        response = extract_last_response(clean)
                        if (response and "⏺" in response
                                and response != self.last_sent
                                and not self._already_sent(response)):
                            _log("AI→BOT", f"pre-approval flush ({len(response)} chars)")
                            await _send_output(app, chat_id, response)
                            _log("BOT→USER", "delivered (pre-approval flush)")
                            self.last_sent = response
                            self._mark_sent(response)

                        # P2-1: 승인 컨텍스트 저장
                        self.last_approval_summary = summarize_approval(clean)
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
                        await _send_approval(app, chat_id, clean)
                        await asyncio.sleep(1)
                        continue

                    if self.awaiting_approval:
                        await asyncio.sleep(1)
                        continue

                    busy_now = is_busy(clean)

                    # Fix 1: busy 진입 엣지에서 직전 ⏺ 응답 flush
                    if busy_now and not self.was_busy:
                        response = extract_last_response(clean)
                        if (response and "⏺" in response
                                and response != self.last_sent
                                and not self._already_sent(response)):
                            _log("AI→BOT", f"pre-busy flush ({len(response)} chars)")
                            await _send_output(app, chat_id, response)
                            _log("BOT→USER", "delivered (pre-busy flush)")
                            self.last_sent = response
                            self._mark_sent(response)

                    # busy 진입
                    if busy_now and not self.was_busy:
                        self.was_busy = True
                        self.busy_started_at = time.time()
                        # P2: busy 진입 시 pane hash 추적 초기화
                        self.busy_pane_hash = hashlib.md5(clean.encode()).hexdigest()
                        self.busy_last_change_at = time.time()
                        self.saw_compaction = False
                        # 잔존 스크롤백의 Crunched/Compacting 마커로 오탐하지 않도록
                        # busy 진입 시점의 상태를 snapshot — 이후 "새로 등장한 경우"에만 감지
                        self._pre_busy_had_compact = has_compaction(clean)
                        label, cc_sec = busy_status(clean)
                        self.last_status_label = label
                        _log("AI-BUSY", label)
                        try:
                            init_text = _format_busy_status(label, cc_sec, 0)
                            msg = await app.bot.send_message(chat_id, init_text)
                            self.status_msg_id = msg.message_id
                        except Exception as e:
                            _log("STATUS-MSG-ERR", str(e))
                            self.status_msg_id = None

                    # busy 지속: 상태 메시지 편집 + watchdog
                    if busy_now:
                        # P1: 압축 이벤트 감지 (한 번만 로그)
                        # busy 진입 시점에 이미 있던 잔존 마커는 무시하고,
                        # 이 cycle에서 '새로' 등장했을 때만 감지.
                        if (
                            not self.saw_compaction
                            and has_compaction(clean)
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

                        if self.status_msg_id and self.busy_started_at:
                            elapsed = int(now_ts - self.busy_started_at)
                            label, cc_sec = busy_status(clean)
                            new_text = _format_busy_status(label, cc_sec, elapsed)
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
                        response = extract_last_response(clean)
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
                                await _send_output(app, chat_id, response)
                                _log("BOT→USER", "delivered (post-compact)")
                            except Exception as e:
                                _log("POST-COMPACT-SEND-FAIL", str(e))
                            self.last_sent = response
                            self._mark_sent(response)
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
                        response = extract_last_response(pending)
                        is_first = not self.last_sent

                        to_send = None
                        if response and "⏺" in response:
                            if response != self.last_sent and not self._already_sent(response):
                                to_send = response
                        elif is_first:
                            to_send = "\n".join(pending.splitlines()[-30:]).strip()

                        if to_send and not self.awaiting_approval:
                            _log("AI→BOT", f"response ({len(to_send)} chars)")
                            await _send_output(app, chat_id, to_send)
                            _log("BOT→USER", "delivered")
                            self.last_sent = to_send
                            self._mark_sent(to_send)
                        else:
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


bridge = Bridge()

# -- 헬퍼 ----------------------------------------------------------------------

def _chunk_text(text: str, size: int = MAX_MSG_CHARS) -> list[str]:
    """긴 텍스트를 줄 단위로 size 이하로 나눔"""
    chunks = []
    buf = []
    cur = 0
    for line in text.splitlines(keepends=True):
        if cur + len(line) > size and buf:
            chunks.append("".join(buf))
            buf = [line]
            cur = len(line)
        else:
            buf.append(line)
            cur += len(line)
        while cur > size:
            s = "".join(buf)
            chunks.append(s[:size])
            remain = s[size:]
            buf = [remain]
            cur = len(remain)
    if buf:
        chunks.append("".join(buf))
    return chunks or [""]

async def _send_output(app: Application, chat_id: int, text: str):
    import html as _html
    chunks = _chunk_text(text)
    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        header = f"[{i}/{total}]\n" if total > 1 else ""
        body = f"{header}<pre>{_html.escape(chunk)}</pre>"
        try:
            await app.bot.send_message(chat_id, body, parse_mode="HTML")
        except Exception as e:
            _log("SEND-HTML-FAIL", str(e))
            try:
                await app.bot.send_message(chat_id, header + chunk)
            except Exception as e2:
                _log("SEND-PLAIN-FAIL", str(e2))
        await asyncio.sleep(0.2)

def _approval_box(text: str) -> str:
    """'Do you want to proceed?' 위쪽 가장 가까운 divider~prompt 사이만 추출."""
    lines = text.splitlines()
    proceed = -1
    for i in range(len(lines) - 1, -1, -1):
        if "Do you want to" in lines[i]:
            proceed = i
            break
    if proceed < 0:
        return text[-400:]
    start = max(0, proceed - APPROVAL_SCAN_LINES)
    for i in range(proceed - 1, start - 1, -1):
        s = lines[i].strip()
        if s and len(s) > 20 and all(c in "─" for c in s):
            start = i + 1
            break
    end = min(len(lines), proceed + 6)
    return "\n".join(lines[start:end]).strip()


async def _send_approval(app: Application, chat_id: int, text: str):
    kb = [[
        InlineKeyboardButton("Yes (승인)", callback_data="approve_yes"),
        InlineKeyboardButton("No (거부)",  callback_data="approve_no"),
    ]]
    snippet = _approval_box(text)
    if len(snippet) > 1200:
        snippet = snippet[-1200:]
    try:
        await app.bot.send_message(
            chat_id,
            f"승인 요청:\n```\n{snippet}\n```",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown",
        )
    except Exception:
        await app.bot.send_message(
            chat_id,
            f"승인 요청:\n{snippet}",
            reply_markup=InlineKeyboardMarkup(kb),
        )


_PICKER_OPT_RE = re.compile(r"^\s*(?P<arrow>❯)?\s*(?P<num>\d+)\.\s+(?P<name>.+?)(?:\s{2,}|$)")


def _parse_model_options(lines: list[str]) -> list[dict]:
    """/model 피커 라인에서 옵션 목록 추출.

    리턴: [{"num": int, "name": str, "current": bool}, ...]
    - 같은 번호가 여러 번 보이면 마지막 것만 유지 (TUI 재렌더 안전)
    """
    found: dict[int, dict] = {}
    for ln in lines:
        m = _PICKER_OPT_RE.match(ln)
        if not m:
            continue
        num = int(m.group("num"))
        name = m.group("name").strip()
        # 긴 설명은 잘라내기
        if len(name) > 40:
            name = name[:40].rstrip()
        found[num] = {
            "num": num,
            "name": name,
            "current": bool(m.group("arrow")),
        }
    return [found[k] for k in sorted(found)]


def _find_picker_cursor(lines: list[str]) -> int | None:
    """현재 ❯ 커서가 가리키는 옵션 번호."""
    for ln in lines:
        m = _PICKER_OPT_RE.match(ln)
        if m and m.group("arrow"):
            return int(m.group("num"))
    return None


async def _send_model_switch_prompt(app: Application, chat_id: int):
    """/compact가 API 에러로 실패 → 표준 모델 전환 버튼 안내."""
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 /model 열기", callback_data="open_model_picker"),
    ]])
    await app.bot.send_message(
        chat_id,
        "⚠️ 자동 압축 실패 — 1M 컨텍스트에 Extra Usage 미활성입니다.\n"
        "버튼을 누르면 `/model` 피커를 열고 현재 화면을 여기로 전달합니다.",
        reply_markup=kb,
    )


async def _render_model_picker(app: Application, chat_id: int, *, already_open: bool):
    """현재 pane의 /model 피커를 캡쳐해 옵션 버튼과 함께 포워딩."""
    pane = pane_output()
    clean = strip_ansi(pane).strip()
    lines = clean.splitlines()
    start = max(0, len(lines) - 40)
    for i in range(len(lines) - 1, start - 1, -1):
        s = lines[i].strip()
        if s and len(s) > 20 and all(c in "─" for c in s):
            start = i + 1
            break
    picker_lines = lines[start:]
    snippet = "\n".join(picker_lines).strip()
    if len(snippet) > 1800:
        snippet = snippet[-1800:]

    options = _parse_model_options(picker_lines)
    kb_rows: list[list[InlineKeyboardButton]] = []
    for opt in options:
        label = f"{opt['num']}. {opt['name']}"
        if opt['current']:
            label += " ✔️"
        kb_rows.append([InlineKeyboardButton(
            label[:64], callback_data=f"pick_model:{opt['num']}"
        )])
    kb_rows.append([InlineKeyboardButton("✖ 취소 (Esc)", callback_data="pick_model:esc")])

    header = "📋 /model 피커 (이미 열려있음):" if already_open else "📋 /model 피커:"
    if options:
        body = (
            f"{header}\n```\n{snippet}\n```\n"
            "버튼으로 선택하세요. (현재 모델에는 ✔️ 표시)"
        )
    else:
        body = (
            f"{header}\n```\n{snippet}\n```\n"
            "버튼 파싱 실패 — 번호/이름을 답장으로 보내세요."
        )
    try:
        await app.bot.send_message(
            chat_id, body,
            reply_markup=InlineKeyboardMarkup(kb_rows),
            parse_mode="Markdown",
        )
    except Exception:
        await app.bot.send_message(
            chat_id, body[:3900],
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )

# -- 핸들러 -------------------------------------------------------------------

def _build_start_kb(sessions: list[dict]) -> InlineKeyboardMarkup:
    kb = []
    for s in sessions:
        proj = f" [{s['project']}]" if s['project'] else ""
        label = f"[{s['mtime']}]{proj} {s['title']}"
        kb.append([InlineKeyboardButton(label, callback_data="resume:" + s["id"])])
    kb.append([InlineKeyboardButton("+ 새 세션 시작", callback_data="new")])
    kb.append([InlineKeyboardButton(bridge.perm_label(), callback_data="toggle_perm")])
    return InlineKeyboardMarkup(kb)


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"내 chat\\_id: `{cid}`\n\nconfig.json의 allowed\\_ids에 이 값을 추가하세요.",
        parse_mode="Markdown"
    )


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await deny(update); return

    if tmux_run(["has-session", "-t", TMUX]).returncode == 0:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("기존 세션 유지 (재연결)", callback_data="reattach")],
            [InlineKeyboardButton("새로 시작 (기존 종료)", callback_data="force_new")],
        ])
        await update.message.reply_text(
            "이미 실행 중인 세션이 있습니다.", reply_markup=kb
        )
        return

    sessions = find_sessions()
    header = "이어할 세션을 선택하거나 새 세션을 시작하세요:" if sessions else "저장된 세션이 없습니다."
    await update.message.reply_text(header, reply_markup=_build_start_kb(sessions))


async def cmd_end(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await deny(update); return
    await update.message.reply_text("세션을 종료합니다.")
    await bridge.stop()


async def cmd_esc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ESC 키 전송 (승인창 취소 / 작업 중단)"""
    if not is_allowed(update):
        await deny(update); return
    if tmux_run(["has-session", "-t", TMUX]).returncode != 0:
        await update.message.reply_text("세션이 없습니다.")
        return
    send_key("Escape")
    _log("USER→AI", "ESC pressed")
    try:
        await update.message.set_reaction("⚡")
    except Exception:
        pass


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/model 피커 열기 또는 이미 열려있으면 현재 화면 포워딩."""
    if not is_allowed(update):
        await deny(update); return
    if tmux_run(["has-session", "-t", TMUX]).returncode != 0:
        await update.message.reply_text("세션이 없습니다.")
        return
    chat_id = update.effective_chat.id
    clean = strip_ansi(pane_output()).strip()
    already = _find_picker_cursor(clean.splitlines()) is not None
    if not already:
        send_input("/model")
        _log("USER→AI", "/model dispatched")
        await asyncio.sleep(0.8)
    else:
        _log("USER→AI", "/model (picker already open)")
    await _render_model_picker(ctx.application, chat_id, already_open=already)


async def cmd_unlock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """이 인스턴스의 세션 락 강제 해제 (본인 chat_id 소유 락만)"""
    if not is_allowed(update):
        await deny(update); return
    my_chat_id = update.effective_chat.id
    locks = _my_locks()
    if not locks:
        await update.message.reply_text("해제할 락이 없습니다.")
        return
    released, denied = [], []
    for session_id, owner_chat_id in locks:
        if owner_chat_id == my_chat_id or owner_chat_id == 0:
            _lock_path(session_id).unlink(missing_ok=True)
            released.append(session_id[:12])
            _log("UNLOCK", f"{session_id[:12]} by chat_id={my_chat_id}")
        else:
            denied.append(session_id[:12])
    lines = []
    if released:
        lines.append(f"✅ 해제됨: {', '.join(released)}")
    if denied:
        lines.append(f"⛔ 권한 없음 (다른 사용자 소유): {', '.join(denied)}")
    await update.message.reply_text("\n".join(lines))


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.callback_query.answer("접근 거부", show_alert=True); return
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "reattach":
        await q.edit_message_text("기존 세션 재연결 중…")
        if bridge.task:
            bridge.task.cancel()
        bridge.task = asyncio.create_task(bridge.monitor(ctx.application, q.message.chat_id))
        return

    if data == "force_new":
        tmux_run(["kill-session", "-t", TMUX])
        sessions = find_sessions()
        header = "이어할 세션을 선택하거나 새 세션을 시작하세요:" if sessions else "저장된 세션이 없습니다."
        await q.edit_message_text(header, reply_markup=_build_start_kb(sessions))
        return

    if data == "approve_yes":
        # P2-2: 현재 pane 상태 확인 후 적절히 처리
        pane = pane_output()
        clean = strip_ansi(pane).strip()
        context = bridge.last_approval_context or "승인"

        if is_approval(clean):
            # 정상 경로: 프롬프트가 여전히 있음
            bridge.awaiting_approval = False
            _log("USER-ACK", "approved")
            send_key("Enter")
            summary = bridge.last_approval_summary or "승인"
            try:
                await q.edit_message_text(f"✅ 승인 · {summary}", reply_markup=None)
            except Exception:
                pass
        elif bridge.is_alive():
            # 프롬프트 만료, 세션 살아있음 → Claude에 재요청
            bridge.awaiting_approval = False
            _log("USER-ACK", f"late approved (prompt expired) — context={context}")
            send_input(f"사용자가 '{context}' 작업을 승인했습니다. 이어서 진행해주세요.")
            try:
                await q.edit_message_text(
                    f"✅ 승인 · {context}\n프롬프트 만료 — Claude에 재요청했습니다",
                    reply_markup=None
                )
            except Exception:
                pass
        elif bridge.current_session_id:
            # 세션 죽음 → 재시작 후 재요청
            bridge.awaiting_approval = False
            _log("USER-ACK", f"late approved (session dead) — context={context}")
            ok = bridge.start(bridge.current_session_id, chat_id=q.message.chat_id)
            if ok:
                if bridge.task:
                    bridge.task.cancel()
                bridge.task = asyncio.create_task(
                    bridge.monitor(ctx.application, q.message.chat_id)
                )
                await asyncio.sleep(3)
                send_input(f"사용자가 '{context}' 작업을 승인했습니다. 이어서 진행해주세요.")
                try:
                    await q.edit_message_text(
                        f"✅ 승인 · {context}\n세션 재시작 후 이어갑니다",
                        reply_markup=None
                    )
                except Exception:
                    pass
            else:
                try:
                    await q.edit_message_text("세션 재시작 실패. /start로 다시 시작하세요.", reply_markup=None)
                except Exception:
                    pass
        else:
            await q.answer("이미 처리됨", show_alert=False)

    elif data == "approve_no":
        # P2-3: 현재 pane 상태 확인 후 적절히 처리
        pane = pane_output()
        clean = strip_ansi(pane).strip()
        context = bridge.last_approval_context or "거부"

        if is_approval(clean):
            # 정상 경로: 프롬프트가 여전히 있음
            bridge.awaiting_approval = False
            _log("USER-ACK", "denied")
            send_key("Down")
            await asyncio.sleep(0.15)
            send_key("Enter")
            summary = bridge.last_approval_summary or "거부"
            try:
                await q.edit_message_text(f"❌ 거부 · {summary}", reply_markup=None)
            except Exception:
                pass
        elif bridge.is_alive():
            # 프롬프트 만료, 세션 살아있음 → Claude에 취소 요청
            bridge.awaiting_approval = False
            _log("USER-ACK", f"late denied (prompt expired) — context={context}")
            send_input(f"사용자가 '{context}' 작업을 거부했습니다. 해당 작업을 취소하고 대기해주세요.")
            try:
                await q.edit_message_text(
                    f"❌ 거부 · {context}\nClaude에 취소 요청했습니다",
                    reply_markup=None
                )
            except Exception:
                pass
        elif bridge.current_session_id:
            # 세션 죽음 → 재시작 여부 묻기
            bridge.awaiting_approval = False
            session_id = bridge.current_session_id
            _log("USER-ACK", f"late denied (session dead) — context={context}")
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("이 세션 재시작", callback_data=f"resume_after_no:{session_id}"),
                InlineKeyboardButton("취소",           callback_data="resume_after_no:cancel"),
            ]])
            try:
                await q.edit_message_text(
                    f"세션이 종료되었습니다.\n마지막 작업: {context}\n이 세션을 다시 시작하시겠습니까?",
                    reply_markup=kb
                )
            except Exception:
                pass
        else:
            await q.answer("이미 처리됨", show_alert=False)

    elif data.startswith("resume_after_no:"):
        # P2-4: resume_after_no 콜백 처리
        val = data[len("resume_after_no:"):]
        if val == "cancel":
            try:
                await q.edit_message_text("취소했습니다.", reply_markup=None)
            except Exception:
                pass
            return
        session_id = val
        ok = bridge.start(session_id, chat_id=q.message.chat_id)
        if ok:
            if bridge.task:
                bridge.task.cancel()
            bridge.task = asyncio.create_task(
                bridge.monitor(ctx.application, q.message.chat_id)
            )
            try:
                await q.edit_message_text("세션을 다시 시작했습니다.", reply_markup=None)
            except Exception:
                pass
        else:
            try:
                await q.edit_message_text("재시작 실패. /start로 다시 시도해주세요.", reply_markup=None)
            except Exception:
                pass

    elif data == "open_model_picker":
        # /compact 실패 → /model 피커를 열고 옵션 버튼으로 포워딩
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        try:
            send_input("/model")
        except Exception as e:
            _log("MODEL-OPEN-FAIL", str(e))
            await q.answer("입력 실패", show_alert=True)
            return
        await asyncio.sleep(0.8)  # TUI 렌더 안정화
        await _render_model_picker(ctx.application, q.message.chat_id, already_open=False)

    elif data.startswith("pick_model:"):
        choice = data.split(":", 1)[1]
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if choice == "esc":
            send_key("Escape")
            _log("USER-ACK", "model picker cancelled")
            try:
                await q.answer("피커 닫음", show_alert=False)
            except Exception:
                pass
            return
        # 현재 pane에서 ❯ 위치 읽어 화살표로 네비 후 Enter
        pane = pane_output()
        clean = strip_ansi(pane).strip()
        cur = _find_picker_cursor(clean.splitlines())
        try:
            target = int(choice)
        except ValueError:
            await q.answer("잘못된 선택", show_alert=True)
            return
        if cur is None:
            # fallback: 숫자 + Enter 시도
            send_input(str(target))
            _log("USER-ACK", f"model pick (fallback) {target}")
        else:
            delta = target - cur
            key = "Down" if delta > 0 else "Up"
            for _ in range(abs(delta)):
                send_key(key)
                await asyncio.sleep(0.08)
            await asyncio.sleep(0.15)
            send_key("Enter")
            _log("USER-ACK", f"model pick {cur}→{target}")
        try:
            await q.answer(f"선택: {target}", show_alert=False)
        except Exception:
            pass


    elif data == "do_unlock":
        # P3-1: 잠금 오류 메시지의 인라인 /unlock 버튼
        my_chat_id = q.message.chat_id
        locks = _my_locks()
        released = []
        for session_id, owner_chat_id in locks:
            if owner_chat_id == my_chat_id or owner_chat_id == 0:
                _lock_path(session_id).unlink(missing_ok=True)
                released.append(session_id[:12])
        if released:
            await q.answer(f"해제됨: {', '.join(released)}", show_alert=True)
        else:
            await q.answer("해제할 수 있는 락이 없습니다.", show_alert=True)

    elif data == "toggle_perm":
        bridge.skip_permissions = not bridge.skip_permissions
        perm_str = "스킵 (--dangerously-skip-permissions)" if bridge.skip_permissions else "확인 (기본)"

        if bridge.running:
            await q.edit_message_text(f"권한 모드 -> {perm_str}\n재시작 중...")
            ok = bridge.restart()
            if ok:
                bridge.task = asyncio.create_task(
                    bridge.monitor(ctx.application, q.message.chat_id)
                )
                await ctx.bot.send_message(q.message.chat_id, f"재시작 완료. 권한 모드: {perm_str}")
            else:
                await ctx.bot.send_message(q.message.chat_id, "재시작 실패")
        else:
            sessions = find_sessions()
            kb = _build_start_kb(sessions) if sessions else InlineKeyboardMarkup([
                [InlineKeyboardButton("+ 새 세션 시작", callback_data="new")],
                [InlineKeyboardButton(bridge.perm_label(), callback_data="toggle_perm")],
            ])
            await q.edit_message_reply_markup(kb)

    elif data == "new" or data.startswith("resume:"):
        session_id = None if data == "new" else data[7:]
        default_cwd = str(Path(__file__).parent)
        cwd = get_session_cwd(session_id) if session_id else default_cwd
        if not cwd:
            cwd = default_cwd

        if session_id:
            label = f"세션 재개 ({session_id[:8]}...)\n폴더: `{cwd}`"
        else:
            label = f"신규 세션\n폴더: `{cwd}`"

        await q.edit_message_text(f"시작 중: {label}", parse_mode="Markdown")
        ok = bridge.start(session_id, chat_id=q.message.chat_id)

        if ok:
            if bridge.task:
                bridge.task.cancel()
            bridge.task = asyncio.create_task(
                bridge.monitor(ctx.application, q.message.chat_id)
            )
        else:
            if session_id and _is_locked(session_id):
                # P3-1: 잠금 오류 메시지 UX 개선 + /unlock 인라인 버튼
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("/unlock 실행", callback_data="do_unlock")
                ]])
                await ctx.bot.send_message(
                    q.message.chat_id,
                    "⚠️ 이미 사용 중인 세션입니다. /unlock으로 해제 후 다시 시도하세요.",
                    reply_markup=kb,
                )
            else:
                await ctx.bot.send_message(
                    q.message.chat_id,
                    "시작 실패 - tmux/claude 경로를 확인하세요."
                )


IMAGE_DIR = Path(__file__).parent / "tg_images"
IMAGE_DIR.mkdir(exist_ok=True)

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await deny(update); return

    session_alive = bridge.running and tmux_run(["has-session", "-t", TMUX]).returncode == 0
    if not session_alive:
        await update.message.reply_text("세션이 실행 중이 아닙니다. /start 로 시작하세요.")
        return

    msg = update.message
    caption = (msg.caption or msg.text or "").strip()

    if msg.photo:
        photo = msg.photo[-1]
        fname = f"tg_{int(time.time())}.jpg"
        fpath = IMAGE_DIR / fname
        try:
            f = await ctx.bot.get_file(photo.file_id)
            await f.download_to_drive(custom_path=str(fpath))
            _log("USER→BOT", f"image {fname} ({photo.file_size or 0} bytes)")

            payload = f"[텔레그램 이미지 첨부: {fpath}]"
            if caption:
                payload += f"\n{caption}"
            send_input(payload)
            _log("BOT→AI", "forwarded image + caption")
            try:
                await msg.set_reaction("📷")
            except Exception:
                pass
            return
        except Exception as e:
            await msg.reply_text(f"이미지 수신 실패: {e}")
            return

    if not caption:
        return
    preview = caption[:60].replace("\n", " ")
    _log("USER→BOT", preview)
    send_input(caption)
    _log("BOT→AI", "forwarded to Claude (waiting for response)")
    try:
        await msg.set_reaction("✍")
    except Exception:
        pass


# -- 진입점 -------------------------------------------------------------------

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",  "세션 목록 / 새 세션 시작"),
        BotCommand("end",    "현재 세션 종료"),
        BotCommand("esc",    "ESC 키 전송 (취소/중단)"),
        BotCommand("model",  "모델 변경 피커 열기"),
        BotCommand("unlock", "세션 락 강제 해제"),
        BotCommand("whoami", "내 chat_id 확인 (관리자 등록용)"),
    ])

    check = tmux_run(["has-session", "-t", TMUX])
    if check.returncode == 0 and ALLOWED_IDS:
        chat_id = next(iter(ALLOWED_IDS))
        await app.bot.send_message(chat_id, "기존 세션에 재연결되었습니다.")
        bridge.task = asyncio.create_task(bridge.monitor(app, chat_id))


def _build_app() -> Application:
    app = (
        Application.builder()
        .token(TOKEN)
        .request(HTTPXRequest(connect_timeout=10, read_timeout=15))  # P1-3
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("end",    cmd_end))
    app.add_handler(CommandHandler("esc",    cmd_esc))
    app.add_handler(CommandHandler("model",  cmd_model))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.PHOTO,
        on_message,
    ))
    return app


def main():
    # P1-2: Telegram 폴링 재연결 루프 (exponential backoff)
    backoff = 1
    retries = 0
    max_retries = 10

    while True:
        try:
            app = _build_app()
            _log("BOOT", f"claude-bridge started (Ctrl+C to quit, attempt={retries + 1})")
            app.run_polling(drop_pending_updates=True)
            break  # 정상 종료 (KeyboardInterrupt 등)
        except KeyboardInterrupt:
            _log("SHUTDOWN", "Ctrl+C — 종료")
            break
        except Exception as e:
            retries += 1
            _log("POLLING-ERROR", f"재연결 시도 {retries}/{max_retries}: {e}")
            if retries >= max_retries:
                _log("POLLING-FATAL", "최대 재시도 초과 — 종료")
                sys.exit(1)
            # 이전 bridge monitor 정리
            if bridge.task:
                bridge.task.cancel()
                bridge.task = None
            bridge.running = False
            wait = min(backoff, 60)
            _log("POLLING-RETRY", f"{wait}초 후 재시도…")
            time.sleep(wait)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
