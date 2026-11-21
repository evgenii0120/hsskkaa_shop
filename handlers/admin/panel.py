from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from config import ADMIN_IDS
from db.models import DeliveryTask, Order, PaymentAttempt, Ticket, User
from handlers.admin.common import PANEL_TEXT, admin_router, back_kb, panel_kb
from handlers.common import edit_or_answer

entry = Router()
router = admin_router()


@entry.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext, session: AsyncSession) -> None:
    telegram_id = message.from_user.id
    is_admin = telegram_id in ADMIN_IDS
    if not is_admin:
        result = await session.execute(select(User.is_admin).where(User.telegram_id == telegram_id))
        is_admin = result.scalar_one_or_none() is True
    if not is_admin:
        await message.answer(texts.ADMIN_DENIED)
        return
    await state.clear()
    await message.answer(PANEL_TEXT, reply_markup=panel_kb())


@router.callback_query(F.data == "adm:panel")
async def cb_panel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit_or_answer(cb, PANEL_TEXT, panel_kb())
    await cb.answer()


@router.callback_query(F.data == "adm:stats")
async def cb_stats(cb: CallbackQuery, session: AsyncSession) -> None:
    users = (await session.execute(select(func.count(User.id)))).scalar_one()
    total_orders = (await session.execute(select(func.count(Order.id)))).scalar_one()
    paid_orders = (await session.execute(
        select(func.count(func.distinct(PaymentAttempt.order_id))).where(PaymentAttempt.status == "succeeded")
    )).scalar_one()
    payment_states = dict((await session.execute(
        select(PaymentAttempt.status, func.count()).group_by(PaymentAttempt.status)
    )).all())
    delivery_states = dict((await session.execute(
        select(DeliveryTask.status, func.count()).group_by(DeliveryTask.status)
    )).all())
    tickets = dict((await session.execute(
        select(Ticket.status, func.count()).group_by(Ticket.status)
    )).all())
    text = (
        "📊 <b>Статистика</b>\n━━━━━━━━━━━━━━━\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"🧾 Заказов: <b>{total_orders}</b>\n"
        f"💳 Заказов с подтверждённой оплатой: <b>{paid_orders}</b>\n\n"
        "<b>Платежи:</b>\n"
        f"   • ожидают: {payment_states.get('pending', 0)}\n"
        f"   • успешные: {payment_states.get('succeeded', 0)}\n"
        f"   • требуют сверки: {payment_states.get('reconciliation_required', 0) + payment_states.get('creation_unknown', 0)}\n\n"
        "<b>Доставка:</b>\n"
        f"   • доставлено: {delivery_states.get('delivered', 0)}\n"
        f"   • ожидает/повтор: {delivery_states.get('pending', 0) + delivery_states.get('retry_wait', 0)}\n"
        f"   • заблокировано: {delivery_states.get('blocked', 0)}\n"
        f"   • обрабатывается: {delivery_states.get('processing', 0)}\n\n"
        "<b>Тикеты:</b>\n"
        f"   • открыто: {tickets.get('open', 0)}\n"
        f"   • в работе: {tickets.get('in_progress', 0)}\n"
        f"   • закрыто: {tickets.get('closed', 0)}"
    )
    await edit_or_answer(cb, text, back_kb())
    await cb.answer()
