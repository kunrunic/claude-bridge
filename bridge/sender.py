"""outbound: Claude → Telegram 사용자 방향 전송.

여기서 정의된 함수는 모두 app.bot 을 통해 Telegram 으로 보낸다.
inbound (cmd_*, on_message, on_callback) 는 `receiver.py` 에 있다.

테스트 패치 지점을 명확히 하기 위해 sibling 모듈 호출은 모두
`<module>.<name>` 접근을 쓴다 (e.g. `tmux.pane_output()`).
"""
from __future__ import annotations

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from . import dump, parser, tmux
from .config import MAX_MSG_CHARS, _log


def _chunk_text(text: str, size: int = MAX_MSG_CHARS) -> list[str]:
    """긴 텍스트를 줄 단위로 size 이하로 나눔."""
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
    dump.event("sender", "send_output", length=len(text), chunks=total, preview=text[:300])
    for i, chunk in enumerate(chunks, 1):
        header = f"[{i}/{total}]\n" if total > 1 else ""
        body = f"{header}<pre>{_html.escape(chunk)}</pre>"
        try:
            await app.bot.send_message(chat_id, body, parse_mode="HTML")
        except Exception as e:
            _log("SEND-HTML-FAIL", str(e))
            dump.event("sender", "send_html_fail", error=str(e)[:200])
            try:
                await app.bot.send_message(chat_id, header + chunk)
            except Exception as e2:
                _log("SEND-PLAIN-FAIL", str(e2))
                dump.event("sender", "send_plain_fail", error=str(e2)[:200])
        await asyncio.sleep(0.2)


async def _send_approval(app: Application, chat_id: int, text: str):
    dump.event("sender", "send_approval", preview=text[-400:])
    kb = [[
        InlineKeyboardButton("Yes (승인)", callback_data="approve_yes"),
        InlineKeyboardButton("No (거부)",  callback_data="approve_no"),
    ]]
    snippet = parser._approval_box(text)
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


async def _send_model_switch_prompt(app: Application, chat_id: int):
    """/compact 가 API 에러로 실패 → 표준 모델 전환 버튼 안내."""
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
    """현재 pane 의 /model 피커를 캡쳐해 옵션 버튼과 함께 포워딩."""
    pane = tmux.pane_output()
    clean = parser.strip_ansi(pane).strip()
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

    options = parser._parse_model_options(picker_lines)
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
