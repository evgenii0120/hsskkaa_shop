from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from db.crud import get_active_product, get_or_create_user
from handlers.common import edit_or_answer
from handlers.registration import start_registration
from services.checkout import prepare_checkout

router = Router()


@router.callback_query(F.data.startswith("pay:"))
async def pay_start(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    product = await get_active_product(session, int(cb.data.split(":")[1]))
    if not product:
        await cb.answer(texts.PRODUCT_NOT_FOUND, show_alert=True)
        return
    await state.clear()
    user = await get_or_create_user(session, cb.from_user)
    if not user.is_registered:
        await start_registration(cb, state, f"pay:{product.id}")
        return
    await cb.answer()  # создание платежа — сетевой вызов, гасим «часики» сразу
    text, markup = await prepare_checkout(session, user, product, cb.bot)
    await edit_or_answer(cb, text, markup)
