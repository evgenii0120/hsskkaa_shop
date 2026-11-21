from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

import keyboards as kb
import texts
from db.crud import count_purchases, get_or_create_user
from db.models import User
from handlers.common import edit_or_answer
from handlers.registration import _EMAIL_RE, _valid_fio, start_registration
from states import ProfileEdit

router = Router()


async def _profile_text(session: AsyncSession, user: User) -> str:
    return texts.profile(user, await count_purchases(session, user.id))


@router.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    user = await get_or_create_user(session, cb.from_user)
    if not user.is_registered:
        await start_registration(cb, state)
        return
    await edit_or_answer(cb, await _profile_text(session, user), kb.profile())
    await cb.answer()


# ── Изменение ФИО / Email ────────────────────────────────────────────────
@router.callback_query(F.data == "edit_fio")
async def edit_fio(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileEdit.fio)
    await edit_or_answer(cb, texts.EDIT_FIO, kb.cancel_edit())
    await cb.answer()


@router.callback_query(F.data == "edit_email")
async def edit_email(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileEdit.email)
    await edit_or_answer(cb, texts.EDIT_EMAIL, kb.cancel_edit())
    await cb.answer()


@router.message(ProfileEdit.fio, F.text)
async def got_fio(message: Message, state: FSMContext, session: AsyncSession) -> None:
    fio = " ".join(message.text.split())
    if not _valid_fio(fio):
        await message.answer(texts.BAD_FIO, reply_markup=kb.cancel_edit())
        return
    user = await get_or_create_user(session, message.from_user)
    user.fio = fio
    await _saved(message, state, session, user)


@router.message(ProfileEdit.email, F.text)
async def got_email(message: Message, state: FSMContext, session: AsyncSession) -> None:
    email = message.text.strip()
    if not _EMAIL_RE.match(email) or len(email) > 254:
        await message.answer(texts.BAD_EMAIL, reply_markup=kb.cancel_edit())
        return
    user = await get_or_create_user(session, message.from_user)
    user.email = email
    await _saved(message, state, session, user)


async def _saved(message: Message, state: FSMContext, session: AsyncSession, user: User) -> None:
    await state.clear()
    await session.flush()
    await message.answer(texts.PROFILE_SAVED)
    await message.answer(await _profile_text(session, user), reply_markup=kb.profile())


@router.message(ProfileEdit.fio)
@router.message(ProfileEdit.email)
async def only_text(message: Message) -> None:
    await message.answer(texts.TEXT_ONLY, reply_markup=kb.cancel_edit())