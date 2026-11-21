from aiogram import Router
from aiogram.types import CallbackQuery, Message

import keyboards as kb
import texts

router = Router()


@router.message()
async def fallback_message(message: Message) -> None:
    await message.answer(texts.FALLBACK, reply_markup=kb.main_menu())


@router.callback_query()
async def fallback_callback(cb: CallbackQuery) -> None:
    # Кнопки админки/тикетов нажал не админ (старое сообщение, пересланная кнопка)
    if (cb.data or "").startswith(("adm:", "tk:")):
        await cb.answer(texts.ADMIN_DENIED, show_alert=True)
        return
    await cb.answer(texts.SESSION_EXPIRED, show_alert=True)