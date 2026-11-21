from html import escape

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, Message
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Ticket, TicketMessage, User
from handlers.admin.common import admin_router
from handlers.common import edit_or_answer
from services.helpdesk import drop_other_take_buttons, extract, preview, send_reply_to_user
from states import TicketReply

router = admin_router()

STATUS = {"open": "🟡 открыт", "in_progress": "🟠 в работе", "closed": "🟢 закрыт"}


def _name(u: User) -> str:
    return escape(u.fio or f"tg:{u.telegram_id}")


async def _view(session: AsyncSession, ticket: Ticket, admin_id: int) -> tuple[str, Kb]:
    user = await session.get(User, ticket.user_id)
    msgs = list((await session.execute(
        select(TicketMessage).where(TicketMessage.ticket_id == ticket.id)
        .order_by(TicketMessage.id.desc()).limit(8)
    )).scalars())[::-1]
    history = "\n".join(
        f"{'👤' if m.sender_id == user.telegram_id else '🛠'} {preview(m.message_type, m.content, 200)}"
        for m in msgs
    )
    text = (
        f"🎫 <b>Тикет №{ticket.id}</b> · {STATUS[ticket.status]}\n"
        f"👤 {_name(user)} · {escape(user.email or '—')} · tg:{user.telegram_id}\n"
        f"━━━━━━━━━━━━━━━\n{history}"
    )
    rows = []
    if ticket.status == "open":
        rows.append([Btn(text="📥 Взять в работу", callback_data=f"tk:take:{ticket.id}")])
    elif ticket.status == "in_progress" and ticket.admin_id == admin_id:
        rows.append([Btn(text="💬 Ответить", callback_data=f"tk:reply:{ticket.id}"),
                     Btn(text="🔒 Закрыть", callback_data=f"tk:close:{ticket.id}")])
    rows.append([Btn(text="⬅️ К списку", callback_data="adm:tickets")])
    return text, Kb(inline_keyboard=rows)


@router.callback_query(F.data == "adm:tickets")
async def cb_list(
    cb: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await state.clear()

    rows = (await session.execute(
        select(Ticket, User)
        .join(User, User.id == Ticket.user_id)
        .where(Ticket.status != "closed")
        .order_by(Ticket.id.desc())
        .limit(30)
    )).all()

    buttons = [
        [
            Btn(
                text=f"{STATUS[t.status][:2]} №{t.id} · "
                     f"{u.fio or 'tg:' + str(u.telegram_id)}"[:60],
                callback_data=f"tk:open:{t.id}",
            )
        ]
        for t, u in rows
    ]

    buttons.append([
        Btn(
            text="🗄 Архив закрытых тикетов",
            callback_data="adm:tickets:archive",
        )
    ])
    buttons.append([
        Btn(text="⬅️ Назад", callback_data="adm:panel")
    ])

    head = (
        "💬 <b>Активные тикеты</b>"
        if rows
        else "💬 <b>Активных тикетов нет</b>"
    )

    await edit_or_answer(cb, head, Kb(inline_keyboard=buttons))
    await cb.answer()

@router.callback_query(F.data == "adm:tickets:archive")
async def cb_ticket_archive(
    cb: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await state.clear()

    rows = (await session.execute(
        select(Ticket, User)
        .join(User, User.id == Ticket.user_id)
        .where(Ticket.status == "closed")
        .order_by(Ticket.id.desc())
        .limit(30)
    )).all()

    buttons = [
        [
            Btn(
                text=f"🟢 №{t.id} · {u.fio or 'tg:' + str(u.telegram_id)}"[:60],
                callback_data=f"tk:open:{t.id}",
            )
        ]
        for t, u in rows
    ]

    buttons.append([
        Btn(text="⬅️ К активным тикетам", callback_data="adm:tickets")
    ])

    head = (
        f"🗄 <b>Архив закрытых тикетов</b>\n"
        f"Всего показано: {len(rows)}"
        if rows
        else "🗄 <b>Архив пуст</b>\nЗакрытых тикетов пока нет."
    )

    await edit_or_answer(cb, head, Kb(inline_keyboard=buttons))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^tk:open:\d+$"))
async def cb_open(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    ticket = await session.get(Ticket, int(cb.data.split(":")[2]))
    if not ticket:
        await cb.answer("Тикет не найден", show_alert=True)
        return
    text, kb = await _view(session, ticket, cb.from_user.id)
    await edit_or_answer(cb, text, kb)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^tk:take:\d+$"))
async def cb_take(cb: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    tid = int(cb.data.split(":")[2])
    # атомарно: взять может только один админ
    res = await session.execute(
        update(Ticket)
        .where(Ticket.id == tid, Ticket.status == "open", Ticket.admin_id.is_(None))
        .values(admin_id=cb.from_user.id, status="in_progress")
    )
    if res.rowcount == 0:
        await cb.answer("Тикет уже взят другим администратором или закрыт.", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    await session.commit()
    await drop_other_take_buttons(bot, tid, cb.from_user.id)
    ticket = await session.get(Ticket, tid)
    await session.refresh(ticket)
    text, kb = await _view(session, ticket, cb.from_user.id)
    await edit_or_answer(cb, text, kb)
    await cb.answer("Тикет взят в работу")


# ── Ответ пользователю ───────────────────────────────────────────────────
async def _own_active(session: AsyncSession, tid: int, admin_id: int) -> Ticket | None:
    t = await session.get(Ticket, tid)
    return t if t and t.status == "in_progress" and t.admin_id == admin_id else None


@router.callback_query(F.data.regexp(r"^tk:reply:\d+$"))
async def cb_reply(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    tid = int(cb.data.split(":")[2])
    if not await _own_active(session, tid, cb.from_user.id):
        await cb.answer("Тикет закрыт или принадлежит другому админу.", show_alert=True)
        return
    await state.set_state(TicketReply.waiting)
    await state.update_data(ticket_id=tid)
    await edit_or_answer(
        cb,
        f"💬 <b>Ответ по тикету №{tid}</b>\n\nОтправьте сообщение (текст, фото, видео, голосовое, стикер, гифка) — "
        "бот передаст его пользователю от своего имени. Можно отправить несколько.",
        Kb(inline_keyboard=[[Btn(text="⏹ Завершить ответ", callback_data=f"tk:open:{tid}")]]),
    )
    await cb.answer()


@router.message(TicketReply.waiting)
async def got_reply(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    tid = (await state.get_data()).get("ticket_id", 0)
    ticket = await _own_active(session, tid, message.from_user.id)
    if not ticket:
        await state.clear()
        await message.answer("❌ Тикет закрыт — отвечать нельзя.")
        return
    if message.text and message.text.startswith("/"):
        await message.answer("Команды недоступны в режиме ответа. Нажмите «Завершить ответ».")
        return
    parsed = extract(message)
    if not parsed:
        await message.answer("⚠️ Этот тип сообщений не поддерживается.")
        return
    mtype, content, file_id = parsed
    user = await session.get(User, ticket.user_id)
    if not await send_reply_to_user(bot, message, user.telegram_id, tid):
        await message.answer("❌ Не удалось доставить (пользователь мог заблокировать бота).")
        return
    session.add(TicketMessage(ticket_id=tid, sender_id=message.from_user.id,
                              message_type=mtype, content=content, file_id=file_id))
    await message.answer("✅ Отправлено пользователю.")


# ── Закрытие ─────────────────────────────────────────────────────────────
@router.callback_query(F.data.regexp(r"^tk:close:\d+$"))
async def cb_close(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    tid = int(cb.data.split(":")[2])
    ticket = await _own_active(session, tid, cb.from_user.id)
    if not ticket:
        await cb.answer("Тикет уже закрыт или принадлежит другому админу.", show_alert=True)
        return
    ticket.status = "closed"  # пользователю ничего не отправляем
    await state.clear()
    text, kb = await _view(session, ticket, cb.from_user.id)
    await edit_or_answer(cb, text, kb)
    await cb.answer("Тикет закрыт")