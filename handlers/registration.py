import re
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

import keyboards as kb
import texts
from db.crud import get_active_product, get_or_create_user
from handlers.common import edit_or_answer
from services.checkout import prepare_checkout
from states import RegistrationFlow

router = Router()

_FIO_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-' .]*$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def _valid_fio(value: str) -> bool:
    return 2 <= len(value.split()) <= 5 and len(value) <= 100 and bool(_FIO_RE.match(value))


async def start_registration(event: Message | CallbackQuery, state: FSMContext, after: str = "menu") -> None:
    """Экран регистрации. after: 'menu' | 'pay:<id>' | 'product:<id>' — куда идти после успеха."""
    await state.clear()
    await state.set_state(RegistrationFlow.consent)
    await state.update_data(agreed=False, after=after)
    if isinstance(event, CallbackQuery):
        await edit_or_answer(event, texts.REGISTRATION, kb.registration(False))
        await event.answer()
    else:
        await event.answer(texts.REGISTRATION, reply_markup=kb.registration(False))


async def _in_consent(cb: CallbackQuery, state: FSMContext) -> bool:
    """Если состояние потеряно (рестарт бота) — показываем экран заново."""
    if await state.get_state() == RegistrationFlow.consent.state:
        return True
    await start_registration(cb, state)
    return False


# ── Чекбокс согласия ─────────────────────────────────────────────────────
@router.callback_query(F.data == "toggle_consent")
async def toggle_consent(cb: CallbackQuery, state: FSMContext) -> None:
    if not await _in_consent(cb, state):
        return
    agreed = not (await state.get_data()).get("agreed", False)
    await state.update_data(agreed=agreed)
    await edit_or_answer(cb, texts.REGISTRATION, kb.registration(agreed))
    await cb.answer()


@router.callback_query(F.data == "do_register")
async def do_register(cb: CallbackQuery, state: FSMContext) -> None:
    if not await _in_consent(cb, state):
        return
    if not (await state.get_data()).get("agreed"):
        await cb.answer(texts.SELECT_CONSENT, show_alert=True)
        return
    await state.set_state(RegistrationFlow.fio)
    await edit_or_answer(cb, texts.ASK_FIO, kb.cancel_registration())
    await cb.answer()


@router.callback_query(F.data == "cancel_reg")
async def cancel_reg(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit_or_answer(cb, texts.MENU, kb.main_menu())
    await cb.answer("Регистрация отменена")


# ── ФИО → Email ──────────────────────────────────────────────────────────
@router.message(RegistrationFlow.fio, F.text)
async def got_fio(message: Message, state: FSMContext) -> None:
    fio = " ".join(message.text.split())
    if not _valid_fio(fio):
        await message.answer(texts.BAD_FIO, reply_markup=kb.cancel_registration())
        return
    await state.update_data(fio=fio)
    await state.set_state(RegistrationFlow.email)
    await message.answer(texts.ASK_EMAIL, reply_markup=kb.cancel_registration())


@router.message(RegistrationFlow.email, F.text)
async def got_email(message: Message, state: FSMContext, session: AsyncSession) -> None:
    email = message.text.strip()
    if not _EMAIL_RE.match(email) or len(email) > 254:
        await message.answer(texts.BAD_EMAIL, reply_markup=kb.cancel_registration())
        return
    data = await state.get_data()
    user = await get_or_create_user(session, message.from_user)
    user.fio, user.email = data["fio"], email
    user.consent_given_at = datetime.now(timezone.utc)
    await state.clear()
    await session.flush()

    await message.answer(texts.REGISTERED)
    kind, _, pid = data.get("after", "menu").partition(":")
    product = await get_active_product(session, int(pid)) if pid.isdigit() else None
    if kind == "pay" and product:
        text, markup = await prepare_checkout(session, user, product, message.bot)
        await message.answer(text, reply_markup=markup)
    elif kind == "product" and product:
        await message.answer(texts.product_card(product), reply_markup=kb.product_card(product.id))
    else:
        await message.answer(texts.MENU, reply_markup=kb.main_menu())


@router.message(RegistrationFlow.fio)
@router.message(RegistrationFlow.email)
async def only_text(message: Message) -> None:
    await message.answer(texts.TEXT_ONLY, reply_markup=kb.cancel_registration())


@router.message(RegistrationFlow.consent)
async def use_buttons(message: Message) -> None:
    await message.answer(texts.USE_BUTTONS)