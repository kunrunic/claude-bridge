"""inbound: Telegram 사용자 → Claude 방향 입력.

cmd_* 슬래시 명령, on_message(텍스트/이미지), on_callback(버튼) 모두 여기 모인다.
들어온 입력은 최종적으로 tmux.send_input / send_key 로 Claude 패널에 주입된다.

sibling 모듈 호출은 `<module>.<name>` 형태로 접근해 테스트 패치 지점을
단일 위치 (`bridge.<module>.<name>`) 로 고정한다.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from . import config, dump, parser, sender, session, tmux
from .config import _log
from .core import bridge

IMAGE_DIR = Path(__file__).parent.parent / "tg_images"
IMAGE_DIR.mkdir(exist_ok=True)


# -- 공통 키보드 --------------------------------------------------------------

def _build_start_kb(sessions: list[dict]) -> InlineKeyboardMarkup:
    kb = []
    for s in sessions:
        proj = f" [{s['project']}]" if s['project'] else ""
        label = f"[{s['mtime']}]{proj} {s['title']}"
        kb.append([InlineKeyboardButton(label, callback_data="resume:" + s["id"])])
    kb.append([InlineKeyboardButton("+ 새 세션 시작", callback_data="new")])
    kb.append([InlineKeyboardButton(bridge.perm_label(), callback_data="toggle_perm")])
    return InlineKeyboardMarkup(kb)


# -- 슬래시 명령 --------------------------------------------------------------

async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"내 chat\\_id: `{cid}`\n\nconfig.json의 allowed\\_ids에 이 값을 추가하세요.",
        parse_mode="Markdown"
    )


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not config.is_allowed(update):
        await config.deny(update); return
    _log("USER→BOT", "/start")

    if tmux.tmux_run(["has-session", "-t", tmux._ts()]).returncode == 0:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("기존 세션 유지 (재연결)", callback_data="reattach")],
            [InlineKeyboardButton("새로 시작 (기존 종료)", callback_data="force_new")],
        ])
        await update.message.reply_text(
            "이미 실행 중인 세션이 있습니다.", reply_markup=kb
        )
        return

    sessions = session.find_sessions()
    header = "이어할 세션을 선택하거나 새 세션을 시작하세요:" if sessions else "저장된 세션이 없습니다."
    await update.message.reply_text(header, reply_markup=_build_start_kb(sessions))


async def cmd_end(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not config.is_allowed(update):
        await config.deny(update); return
    _log("USER→BOT", "/end")
    await update.message.reply_text("세션을 종료합니다.")
    await bridge.stop()


async def cmd_esc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ESC 키 전송 (승인창 취소 / 작업 중단)."""
    if not config.is_allowed(update):
        await config.deny(update); return
    _log("USER→BOT", "/esc")
    if tmux.tmux_run(["has-session", "-t", tmux._ts()]).returncode != 0:
        await update.message.reply_text("세션이 없습니다.")
        return
    tmux.send_key("Escape")
    _log("USER→AI", "ESC pressed")
    # /esc 는 사용자가 직접 승인 모달을 닫은 것 — 봇 쪽 대기 상태도 즉시 해제.
    # 그대로 두면 monitor 가 awaiting_approval=True 분기에서 계속 sleep 하며
    # Claude 의 post-ESC 응답을 보지 못한다. (20260418_094744)
    if bridge.awaiting_approval:
        bridge.awaiting_approval = False
        _log("USER-ACK", "esc (local approval state cleared)")
    try:
        await update.message.set_reaction("⚡")
    except Exception:
        pass


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/model 피커 열기 또는 이미 열려있으면 현재 화면 포워딩."""
    if not config.is_allowed(update):
        await config.deny(update); return
    _log("USER→BOT", "/model")
    if tmux.tmux_run(["has-session", "-t", tmux._ts()]).returncode != 0:
        await update.message.reply_text("세션이 없습니다.")
        return
    chat_id = update.effective_chat.id
    clean = parser.strip_ansi(tmux.pane_output()).strip()
    already = parser._find_picker_cursor(clean.splitlines()) is not None
    if not already:
        tmux.send_input("/model")
        _log("USER→AI", "/model dispatched")
        await asyncio.sleep(0.8)
    else:
        _log("USER→AI", "/model (picker already open)")
    await sender._render_model_picker(ctx.application, chat_id, already_open=already)


async def cmd_unlock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """이 인스턴스의 세션 락 강제 해제 (본인 chat_id 소유 락만)."""
    if not config.is_allowed(update):
        await config.deny(update); return
    _log("USER→BOT", "/unlock")
    my_chat_id = update.effective_chat.id
    locks = session._my_locks()
    if not locks:
        await update.message.reply_text("해제할 락이 없습니다.")
        return
    released, denied = [], []
    for session_id, owner_chat_id in locks:
        if owner_chat_id == my_chat_id or owner_chat_id == 0:
            session._lock_path(session_id).unlink(missing_ok=True)
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


# -- 콜백 (버튼) --------------------------------------------------------------

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not config.is_allowed(update):
        await update.callback_query.answer("접근 거부", show_alert=True); return
    q = update.callback_query
    await q.answer()
    data = q.data
    dump.event("receiver", "callback", data=data, chat_id=q.message.chat_id if q.message else None)

    if data == "reattach":
        await q.edit_message_text("기존 세션 재연결 중…")
        if bridge.task:
            bridge.task.cancel()
        bridge.task = asyncio.create_task(bridge.monitor(ctx.application, q.message.chat_id))
        return

    if data == "force_new":
        tmux.tmux_run(["kill-session", "-t", tmux._ts()])
        sessions = session.find_sessions()
        header = "이어할 세션을 선택하거나 새 세션을 시작하세요:" if sessions else "저장된 세션이 없습니다."
        await q.edit_message_text(header, reply_markup=_build_start_kb(sessions))
        return

    if data == "approve_yes":
        # P2-2: 현재 pane 상태 확인 후 적절히 처리
        pane = tmux.pane_output()
        clean = parser.strip_ansi(pane).strip()
        context = bridge.last_approval_context or "승인"

        if parser.is_approval(clean):
            # 정상 경로: 프롬프트가 여전히 있음
            bridge.awaiting_approval = False
            _log("USER-ACK", "approved")
            tmux.send_key("Enter")
            summary = bridge.last_approval_summary or "승인"
            try:
                await q.edit_message_text(f"✅ 승인 · {summary}", reply_markup=None)
            except Exception:
                pass
        elif bridge.is_alive():
            # 프롬프트 만료, 세션 살아있음 → Claude 에 재요청
            bridge.awaiting_approval = False
            _log("USER-ACK", f"late approved (prompt expired) — context={context}")
            bridge.queue.reset()
            tmux.send_input(f"사용자가 '{context}' 작업을 승인했습니다. 이어서 진행해주세요.")
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
                bridge.queue.reset()
                tmux.send_input(f"사용자가 '{context}' 작업을 승인했습니다. 이어서 진행해주세요.")
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
        pane = tmux.pane_output()
        clean = parser.strip_ansi(pane).strip()
        context = bridge.last_approval_context or "거부"

        if parser.is_approval(clean):
            # 정상 경로: 프롬프트가 여전히 있음
            bridge.awaiting_approval = False
            _log("USER-ACK", "denied")
            tmux.send_key("Down")
            await asyncio.sleep(0.15)
            tmux.send_key("Enter")
            summary = bridge.last_approval_summary or "거부"
            try:
                await q.edit_message_text(f"❌ 거부 · {summary}", reply_markup=None)
            except Exception:
                pass
        elif bridge.is_alive():
            # 프롬프트 만료, 세션 살아있음 → Claude 에 취소 요청
            bridge.awaiting_approval = False
            _log("USER-ACK", f"late denied (prompt expired) — context={context}")
            bridge.queue.reset()
            tmux.send_input(f"사용자가 '{context}' 작업을 거부했습니다. 해당 작업을 취소하고 대기해주세요.")
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
            tmux.send_input("/model")
        except Exception as e:
            _log("MODEL-OPEN-FAIL", str(e))
            await q.answer("입력 실패", show_alert=True)
            return
        await asyncio.sleep(0.8)  # TUI 렌더 안정화
        await sender._render_model_picker(ctx.application, q.message.chat_id, already_open=False)

    elif data.startswith("pick_model:"):
        choice = data.split(":", 1)[1]
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if choice == "esc":
            tmux.send_key("Escape")
            _log("USER-ACK", "model picker cancelled")
            try:
                await q.answer("피커 닫음", show_alert=False)
            except Exception:
                pass
            return
        # 현재 pane 에서 ❯ 위치 읽어 화살표로 네비 후 Enter
        pane = tmux.pane_output()
        clean = parser.strip_ansi(pane).strip()
        cur = parser._find_picker_cursor(clean.splitlines())
        try:
            target = int(choice)
        except ValueError:
            await q.answer("잘못된 선택", show_alert=True)
            return
        if cur is None:
            # fallback: 숫자 + Enter 시도
            tmux.send_input(str(target))
            _log("USER-ACK", f"model pick (fallback) {target}")
        else:
            delta = target - cur
            key = "Down" if delta > 0 else "Up"
            for _ in range(abs(delta)):
                tmux.send_key(key)
                await asyncio.sleep(0.08)
            await asyncio.sleep(0.15)
            tmux.send_key("Enter")
            _log("USER-ACK", f"model pick {cur}→{target}")
        try:
            await q.answer(f"선택: {target}", show_alert=False)
        except Exception:
            pass

    elif data == "do_unlock":
        # P3-1: 잠금 오류 메시지의 인라인 /unlock 버튼
        my_chat_id = q.message.chat_id
        locks = session._my_locks()
        released = []
        for session_id, owner_chat_id in locks:
            if owner_chat_id == my_chat_id or owner_chat_id == 0:
                session._lock_path(session_id).unlink(missing_ok=True)
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
            sessions = session.find_sessions()
            kb = _build_start_kb(sessions) if sessions else InlineKeyboardMarkup([
                [InlineKeyboardButton("+ 새 세션 시작", callback_data="new")],
                [InlineKeyboardButton(bridge.perm_label(), callback_data="toggle_perm")],
            ])
            await q.edit_message_reply_markup(kb)

    elif data == "new" or data.startswith("resume:"):
        session_id = None if data == "new" else data[7:]
        default_cwd = str(Path(__file__).parent.parent)
        cwd = session.get_session_cwd(session_id) if session_id else default_cwd
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
            if session_id and session._is_locked(session_id):
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


# -- 텍스트/이미지 메시지 -----------------------------------------------------

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not config.is_allowed(update):
        await config.deny(update); return

    session_alive = bridge.running and tmux.tmux_run(["has-session", "-t", tmux._ts()]).returncode == 0
    if not session_alive:
        await update.message.reply_text("세션이 실행 중이 아닙니다. /start 로 시작하세요.")
        return

    msg = update.message
    caption = (msg.caption or msg.text or "").strip()
    dump.event(
        "receiver", "on_message",
        has_photo=bool(msg.photo),
        caption=caption[:300],
        length=len(caption),
    )

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
            # 새 turn 시작 — 이전 turn 에서 소비된 ⏺ 커서를 초기화
            bridge.queue.reset()
            tmux.send_input(payload)
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
    # 새 turn 시작 — 이전 turn 에서 소비된 ⏺ 커서를 초기화
    bridge.queue.reset()
    tmux.send_input(caption)
    _log("BOT→AI", "forwarded to Claude (waiting for response)")
    try:
        await msg.set_reaction("✍")
    except Exception:
        pass
