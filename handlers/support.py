from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import keyboards as kb
import texts
from db.crud import get_or_create_user
from db.models import Ticket, TicketMessage
from handlers.common import edit_or_answer
from services.helpdesk import extract, notify_admins
from states import SupportFlow

router = Router()


@router.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit_or_answer(cb, texts.SUPPORT, kb.support())
    await cb.answer()


@router.callback_query(F.data == "support_write")
async def cb_support_write(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SupportFlow.waiting_message)
    # новое сообщение, чтобы не затирать ответ поддержки, к которому прикреплена кнопка
    await cb.message.answer(texts.SUPPORT_ASK, reply_markup=kb.support_cancel())
    await cb.answer()


@router.message(SupportFlow.waiting_message)
async def got_support_message(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    parsed = extract(message)
    if not parsed:
        return
    mtype, content, file_id = parsed

    user = await get_or_create_user(session, message.from_user)
    ticket = (
        await session.execute(
            select(Ticket)
            .where(Ticket.user_id == user.id, Ticket.status.in_(("open", "in_progress")))
            .order_by(Ticket.id.desc())
        )
    ).scalars().first()
    is_new = ticket is None
    if is_new:
        ticket = Ticket(user_id=user.id, status="open")
        session.add(ticket)
        await session.flush()
    session.add(TicketMessage(
        ticket_id=ticket.id, sender_id=message.from_user.id,
        message_type=mtype, content=content, file_id=file_id,
    ))
    await session.commit()  # фиксируем до уведомления админов

    await state.clear()
    await message.answer(texts.SUPPORT_SENT, reply_markup=kb.back_to_menu())
    await notify_admins(bot, ticket, user.fio or message.from_user.full_name, message, mtype, content, is_new)