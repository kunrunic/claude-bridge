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
from pathlib import Path
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

# -- 설정 ----------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent / "config.json"

def _load_config() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

_cfg = _load_config()

TOKEN       = _cfg["token"]
TMUX        = _cfg.get("tmux_session", "claude_bridge")
CLAUDE      = _cfg["claude_path"]
PROJECTS    = Path.home() / ".claude" / "projects"
ALLOWED_IDS: set[int] = set(_cfg.get("allowed_ids", []))

def _log(tag: str, msg: str = ""):
    """구조화된 포그라운드 로그"""
    import time
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
ANSI_RE     = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# -- tmux 유틸 -----------------------------------------------------------------

def tmux_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["/opt/homebrew/bin/tmux"] + cmd,
                          capture_output=True, text=True)

def pane_output() -> str:
    """현재 tmux 패널 내용 반환 (마지막 200줄)"""
    return tmux_run(["capture-pane", "-t", TMUX, "-p", "-S", "-200"]).stdout

def send_input(text: str):
    """Claude에 텍스트 입력 후 Enter (literal 모드로 안전하게)"""
    import time
    # 텍스트는 literal로 전송 (Enter 같은 키명 파싱 회피)
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

    start = max(0, proceed_idx - 30)
    for ln in raw_lines[start:proceed_idx]:
        s = ln.strip()
        m = re.match(r"^(Bash|Edit|Write|Read|MultiEdit|WebFetch|Grep|Glob|Task)\b", s)
        if m:
            return m.group(1)
    return "승인"

def is_busy(text: str) -> bool:
    """Claude가 처리 중인지 - 마지막 20줄만 체크"""
    tail = "\n".join(text.splitlines()[-20:])
    return bool(BUSY_RE.search(tail))

_STATUS_LINE_RE = re.compile(
    r"(?:[·✻⋯*]\s*)?"
    r"(Compacting[^\n│]*|Thinking[^\n│]*|Cerebrating[^\n│]*|Pondering[^\n│]*|"
    r"Cogitat\w+[^\n│]*|Crunching[^\n│]*|Running[^\n│]*|Tool use[^\n│]*)",
    re.IGNORECASE,
)

def busy_status(text: str) -> str:
    """화면에서 실제 진행 상태 라인을 추출"""
    tail_lines = text.splitlines()[-25:]
    # 뒤에서부터 status 라인 검색
    for line in reversed(tail_lines):
        m = _STATUS_LINE_RE.search(line)
        if m:
            label = m.group(1).strip()
            # "esc to interrupt" 꼬리 제거
            for cut in ("esc to interrupt", "ctrl+"):
                idx = label.lower().find(cut)
                if idx > 0:
                    label = label[:idx].strip()
            return label[:80]
    return "작업 중"

def extract_last_response(text: str) -> str:
    """화면 출력에서 Claude의 마지막 응답만 추출"""
    lines = text.splitlines()

    def is_divider(line: str) -> bool:
        s = line.strip()
        return bool(s) and len(s) > 20 and all(c in "─" for c in s)

    end = len(lines)

    # 1) 승인 박스/입력창 경계 감지 — "Do you want to proceed?" 또는 " Bash command" 같은 tool 헤더 직전의 divider 찾기
    #    범위: 전체 (승인 박스는 길 수 있음)
    prompt_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if "Do you want to proceed" in line or line == "Do you want to":
            prompt_idx = i
            break
    if prompt_idx > 0:
        # proceed 위로 올라가며 첫 divider 찾기 → 승인 박스 시작점
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
        return ""   # Claude 응답 없음

    # ⏺ 이후에 새 ❯ 사용자 입력이 있으면 그 전까지만
    for i in range(start + 1, end):
        if lines[i].lstrip().startswith("❯ "):
            end = i
            break

    return "\n".join(lines[start:end]).strip()

# -- 세션 탐색 -----------------------------------------------------------------

def find_sessions(limit: int = 8) -> list[dict]:
    import time
    now = time.time()
    candidates = []
    for p in PROJECTS.rglob("*.jsonl"):
        if p.stem.startswith("agent-"):
            continue
        title, last, last_ts = _parse_session_msgs(p)
        if not title:
            continue
        # last_ts가 없으면 파일 mtime으로 fallback
        activity_ts = last_ts if last_ts > 0 else p.stat().st_mtime
        # 최근 60초 내 활동은 현재 활성 세션 가능성 → 제외
        if now - activity_ts < 60:
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
                    # timestamp는 user/assistant 모든 entry에서 추출
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
        self._sent_keys: list[str] = []   # 최근 전송 응답 키 (중복 방지)

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

        # 폭 80 cols (표준 터미널, 모바일 친화적)
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

    def start(self, session_id: str | None = None) -> bool:
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
        """최근 전송 내역(최대 20개)과 비교해 중복인지 확인"""
        key = self._response_key(text)
        return key in self._sent_keys

    def _mark_sent(self, text: str):
        key = self._response_key(text)
        self._sent_keys.append(key)
        # 최근 20개만 유지
        if len(self._sent_keys) > 20:
            self._sent_keys.pop(0)

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
        send_input("/exit")
        await asyncio.sleep(1.5)
        tmux_run(["kill-session", "-t", TMUX])

    async def monitor(self, app: Application, chat_id: int):
        self.chat_id = chat_id
        self.running = True
        self.last_hash = self.last_sent = ""
        self.dead_reported = False
        self.was_busy = False
        self.status_msg_id: int | None = None    # 진행 상태 메시지 ID
        self.busy_started_at: float | None = None
        self.last_status_label = ""
        settle = 0
        pending = ""

        # 부팅 시점에 pane에 이미 있던 ⏺ 응답은 "이미 사용자가 본 것"으로 간주해
        # 재전송하지 않도록 시드한다 (다중 인스턴스/재기동 대비).
        try:
            seed_clean = strip_ansi(pane_output()).strip()
            seed_resp  = extract_last_response(seed_clean)
            if seed_resp:
                self.last_sent = seed_resp
                self._mark_sent(seed_resp)
                _log("BOOT-SEED", f"마지막 ⏺ 블록 {len(seed_resp)}자 무시 처리")
        except Exception as e:
            print(f"[boot seed error] {e}")

        while self.running:
            try:
                check = tmux_run(["has-session", "-t", TMUX])
                if check.returncode != 0:
                    if not self.dead_reported:
                        await app.bot.send_message(chat_id, "tmux 세션이 사라졌습니다. /start로 다시 시작하세요.")
                        self.dead_reported = True
                        self.running = False
                    break

                if not self.is_alive():
                    if not self.dead_reported:
                        out = pane_output()
                        clean = strip_ansi(out).strip()
                        await app.bot.send_message(
                            chat_id,
                            f"Claude 프로세스가 종료되었습니다.\n마지막 출력:\n```\n{clean[-1500:]}\n```",
                            parse_mode="Markdown"
                        )
                        self.dead_reported = True
                        self.running = False
                    break

                import time
                out = pane_output()
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

                if is_approval(clean) and not self.awaiting_approval:
                    # Fix 2: 승인창 위 새 ⏺ 응답이 있으면 먼저 전송
                    # pre-busy flush 에서 이미 보냈으면 중복 방지
                    response = extract_last_response(clean)
                    if (response and "⏺" in response
                            and response != self.last_sent
                            and not self._already_sent(response)):
                        _log("AI→BOT", f"pre-approval flush ({len(response)} chars)")
                        await _send_output(app, chat_id, response)
                        _log("BOT→USER", "delivered (pre-approval flush)")
                        self.last_sent = response
                        self._mark_sent(response)

                    self.last_approval_summary = summarize_approval(clean)
                    _log("AI-APPROVAL", self.last_approval_summary)
                    # busy 상태 메시지 삭제
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

                # 승인 대기 중이면 busy 상태 처리 스킵
                if self.awaiting_approval:
                    await asyncio.sleep(1)
                    continue

                busy_now = is_busy(clean)

                # Fix 1: busy 진입 엣지에서 직전 ⏺ 응답 flush
                # (연쇄 tool 호출 사이에 낀 중간 설명이 drop 되는 것 방지)
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
                    label = busy_status(clean)
                    self.last_status_label = label
                    _log("AI-BUSY", label)
                    try:
                        msg = await app.bot.send_message(chat_id, f"⏳ {label}… (0s)")
                        self.status_msg_id = msg.message_id
                    except Exception:
                        self.status_msg_id = None

                # busy 지속: 상태 메시지 편집
                if busy_now:
                    if self.status_msg_id and self.busy_started_at:
                        elapsed = int(time.time() - self.busy_started_at)
                        label = busy_status(clean)
                        new_text = f"⏳ {label}… ({elapsed}s)"
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

                # 대기 상태 → 2초 안정 후 전송
                h = hashlib.md5(clean.encode()).hexdigest()
                if h != self.last_hash:
                    self.last_hash = h
                    pending = clean
                    settle = 0
                else:
                    settle += 1

                if pending and settle >= 2 and pending != self.last_sent:
                    response = extract_last_response(pending)
                    is_first = not self.last_sent

                    # 전송 대상 결정:
                    # - ⏺ 응답이 있으면 그것만 (추출본)
                    # - 첫 캡처이고 ⏺가 없으면 현재 화면(30줄)
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

            except Exception as e:
                print(f"[monitor error] {e}")

            await asyncio.sleep(1)


bridge = Bridge()

# -- 헬퍼 ----------------------------------------------------------------------

def _chunk_text(text: str, size: int = 3500) -> list[str]:
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
        # 한 줄이 size보다 크면 강제 분할
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
    chunks = _chunk_text(text, size=3500)
    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        header = f"[{i}/{total}]\n" if total > 1 else ""
        # HTML <pre>는 Markdown의 `_`, `*` 충돌 없음, < > & 만 이스케이프
        body = f"{header}<pre>{_html.escape(chunk)}</pre>"
        try:
            await app.bot.send_message(chat_id, body, parse_mode="HTML")
        except Exception as e:
            print(f"[send HTML failed: {e}]")
            try:
                await app.bot.send_message(chat_id, header + chunk)
            except Exception as e2:
                print(f"[send plain failed: {e2}]")
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
    start = max(0, proceed - 30)
    for i in range(proceed - 1, start - 1, -1):
        s = lines[i].strip()
        if s and len(s) > 20 and all(c in "─" for c in s):
            start = i + 1
            break
    end = min(len(lines), proceed + 6)  # Yes/No/Esc 안내 몇 줄 포함
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
        f"내 chat\_id: `{cid}`\n\nconfig.json의 allowed\_ids에 이 값을 추가하세요.",
        parse_mode="Markdown"
    )


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await deny(update); return

    # 기존 tmux 세션이 살아있으면 안내
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
        if not bridge.awaiting_approval:
            await q.answer("이미 처리됨", show_alert=False)
            return
        bridge.awaiting_approval = False
        _log("USER-ACK", "approved")
        send_key("Enter")
        summary = bridge.last_approval_summary or "승인"
        try:
            await q.edit_message_text(f"✅ 승인 · {summary}", reply_markup=None)
        except Exception:
            pass

    elif data == "approve_no":
        if not bridge.awaiting_approval:
            await q.answer("이미 처리됨", show_alert=False)
            return
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
        ok = bridge.start(session_id)

        if ok:
            if bridge.task:
                bridge.task.cancel()
            bridge.task = asyncio.create_task(
                bridge.monitor(ctx.application, q.message.chat_id)
            )
        else:
            await ctx.bot.send_message(q.message.chat_id, "시작 실패 - tmux/claude 경로를 확인하세요.")


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

    # 이미지 처리
    if msg.photo:
        photo = msg.photo[-1]   # 가장 큰 해상도
        import time
        fname = f"tg_{int(time.time())}.jpg"
        fpath = IMAGE_DIR / fname
        try:
            f = await ctx.bot.get_file(photo.file_id)
            await f.download_to_drive(custom_path=str(fpath))
            _log("USER→BOT", f"image {fname} ({photo.file_size or 0} bytes)")

            # Claude에 경로 + 캡션 전달
            payload = f"[텔레그램 이미지 첨부: {fpath}]"
            if caption:
                payload += f"\n{caption}"
            send_input(payload)
            _log("BOT→AI", f"forwarded image + caption")
            try:
                await msg.set_reaction("📷")
            except Exception:
                pass
            return
        except Exception as e:
            await msg.reply_text(f"이미지 수신 실패: {e}")
            return

    # 일반 텍스트
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
        BotCommand("whoami", "내 chat_id 확인 (관리자 등록용)"),
    ])

    # 기존 tmux 세션이 살아있으면 자동 재연결
    check = tmux_run(["has-session", "-t", TMUX])
    if check.returncode == 0 and ALLOWED_IDS:
        chat_id = next(iter(ALLOWED_IDS))
        await app.bot.send_message(chat_id, "기존 세션에 재연결되었습니다.")
        bridge.task = asyncio.create_task(bridge.monitor(app, chat_id))


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("end",    cmd_end))
    app.add_handler(CommandHandler("esc",    cmd_esc))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.PHOTO,
        on_message,
    ))

    _log("BOOT", "claude-bridge started (Ctrl+C to quit)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
