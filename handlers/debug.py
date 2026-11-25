import logging
import time
import traceback
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, ErrorEvent, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, Message,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config import DEBUG_ADMIN_IDS
from handlers.common import edit_or_answer
from services import debug

router = Router()

_only_debug_admin = F.from_user.id.in_(DEBUG_ADMIN_IDS)
_PANEL = "🐞 <b>Debug-панель</b>\n\nВыберите действие 👇"


def _kb() -> Kb:
    return Kb(inline_keyboard=[
        [Btn(text="📜 Последние ошибки", callback_data="dbg:last"),
         Btn(text="🩺 Статус", callback_data="dbg:status")],
        [Btn(text="🧪 Тестовая ошибка", callback_data="dbg:test"),
         Btn(text="🗑 Очистить", callback_data="dbg:clear")],
    ])


def _back() -> Kb:
    return Kb(inline_keyboard=[[Btn(text="⬅️ Назад", callback_data="dbg:panel")]])


@router.errors()
async def on_error(event: ErrorEvent, bot: Bot) -> bool:
    exc = event.exception
    logging.getLogger("aiogram.event").error("Unhandled error", exc_info=exc)
    upd = event.update
    ctx = "?"
    if upd.callback_query:
        q = upd.callback_query
        ctx = f"callback '{q.data}' от {q.from_user.id}"
    elif upd.message:
        m = upd.message
        ctx = f"message '{(m.text or m.caption or str(m.content_type))[:80]}' от {m.from_user.id}"
    tb = "".join(traceback.format_exception(exc))
    await debug.report(bot, f"{type(exc).__name__} · {ctx}", tb)

    try:
        if upd.callback_query:
            await upd.callback_query.answer("⚠️ Произошла ошибка. Попробуйте позже.", show_alert=True)
        elif upd.message:
            await upd.message.answer("⚠️ Произошла ошибка. Попробуйте позже.")
    except Exception:
        pass
    return True


@router.message(Command("debug"), _only_debug_admin)
async def cmd_debug(message: Message) -> None:
    await message.answer(_PANEL, reply_markup=_kb())


@router.callback_query(F.data == "dbg:panel", _only_debug_admin)
async def cb_panel(cb: CallbackQuery) -> None:
    await edit_or_answer(cb, _PANEL, _kb())
    await cb.answer()


@router.callback_query(F.data == "dbg:last", _only_debug_admin)
async def cb_last(cb: CallbackQuery) -> None:
    if not debug.errors:
        body = "Ошибок нет ✅"
    else:
        body = "\n\n".join(
            f"<b>{escape(t)}</b>\n<pre>{escape(e.splitlines()[0] + chr(10) + e.strip().splitlines()[-1])}</pre>"
            for t, e in list(debug.errors)[-5:]
        )
    await edit_or_answer(cb, f"📜 <b>Последние ошибки</b> (до 5)\n\n{body}"[:4000], _back())
    await cb.answer()


@router.callback_query(F.data == "dbg:status", _only_debug_admin)
async def cb_status(cb: CallbackQuery, session: AsyncSession) -> None:
    try:
        await session.execute(text("SELECT 1"))
        db = "✅ ок"
    except Exception as e:  # noqa: BLE001
        db = f"❌ {type(e).__name__}"
    up = int(time.time() - debug.STARTED_AT)
    msg = (
        "🩺 <b>Статус</b>\n━━━━━━━━━━━━━━━\n"
        f"⏱ Аптайм: {up // 3600}ч {up % 3600 // 60}м {up % 60}с\n"
        f"🗄 БД: {db}\n"
        f"🐞 Ошибок в буфере: {len(debug.errors)}"
    )
    await edit_or_answer(cb, msg, _back())
    await cb.answer()


@router.callback_query(F.data == "dbg:test", _only_debug_admin)
async def cb_test(cb: CallbackQuery) -> None:
    raise RuntimeError("Тестовая ошибка из debug-панели")


@router.callback_query(F.data == "dbg:clear", _only_debug_admin)
async def cb_clear(cb: CallbackQuery) -> None:
    debug.errors.clear()
    await cb.answer("Буфер очищен", show_alert=True)