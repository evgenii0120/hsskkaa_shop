import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from html import escape
from db.models import Order, PaymentAttempt, Product
from handlers.registration import start_registration

import keyboards as kb
import texts
from db.crud import active_products, get_active_product, get_or_create_user
from handlers.common import edit_or_answer

router = Router()

# /start 1  или  /start product_1
_DEEP_LINK = re.compile(r"^(?:product_)?(\d+)$")


@router.message(CommandStart(deep_link=True))
async def start_deep_link(message: Message, command: CommandObject, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    user = await get_or_create_user(session, message.from_user)
    match = _DEEP_LINK.match(command.args or "")
    product = await get_active_product(session, int(match.group(1))) if match else None
    if not user.is_registered:
        await start_registration(message, state, f"product:{product.id}" if product else "menu")
        return
    if product:
        await message.answer(texts.product_card(product), reply_markup=kb.product_card(product.id))
    else:
        await message.answer(texts.PRODUCT_NOT_FOUND)
        await message.answer(texts.WELCOME, reply_markup=kb.main_menu())


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    user = await get_or_create_user(session, message.from_user)
    if not user.is_registered:
        await start_registration(message, state)
        return
    await message.answer(texts.WELCOME, reply_markup=kb.main_menu())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.MENU, reply_markup=kb.main_menu())


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit_or_answer(cb, texts.MENU, kb.main_menu())
    await cb.answer()


@router.callback_query(F.data == "catalog")
async def cb_catalog(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    products = await active_products(session)
    if not products:
        await edit_or_answer(cb, texts.CATALOG_EMPTY, kb.back_to_menu())
    else:
        await edit_or_answer(cb, texts.CATALOG, kb.catalog(products))
    await cb.answer()


@router.callback_query(F.data.startswith("product:"))
async def cb_product(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    product = await get_active_product(session, int(cb.data.split(":")[1]))
    if not product:
        await cb.answer(texts.PRODUCT_NOT_FOUND, show_alert=True)
        return
    await edit_or_answer(cb, texts.product_card(product), kb.product_card(product.id))
    await cb.answer()


@router.callback_query(F.data == "purchases")
async def cb_purchases(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    orders = (await session.execute(
        select(Order).join(PaymentAttempt, PaymentAttempt.order_id == Order.id)
        .where(Order.user_id == (await get_or_create_user(session, cb.from_user)).id,
               PaymentAttempt.status == "succeeded")
        .distinct().order_by(Order.created_at.desc()).limit(30)
    )).scalars().all()
    if not orders:
        body = texts.MY_PURCHASES
    else:
        lines = ["📦 <b>Мои покупки</b>", ""]
        for order in orders:
            product = await session.get(Product, order.product_id)
            if product:
                lines.append(
                    f"• <b>Заказ #{order.id}</b> — {escape(product.title)}\n"
                    f"  {texts.price(order.amount)} · {texts.fmt_dt(order.created_at)}"
                )
        body = "\n".join(lines)
    await edit_or_answer(cb, body, kb.back_to_catalog())
    await cb.answer()


@router.callback_query(F.data == "about")
async def cb_about(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit_or_answer(cb, texts.ABOUT, kb.back_to_catalog())
    await cb.answer()


@router.callback_query(F.data == "docs")
async def cb_docs(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit_or_answer(cb, texts.DOCS, kb.docs())
    await cb.answer()