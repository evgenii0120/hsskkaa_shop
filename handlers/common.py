from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


async def edit_or_answer(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует сообщение с кнопкой; если нельзя — отправляет новое."""
    msg = cb.message
    if isinstance(msg, Message):
        try:
            await msg.edit_text(text, reply_markup=kb)
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return
        await msg.answer(text, reply_markup=kb)
    else:
        await cb.bot.send_message(cb.from_user.id, text, reply_markup=kb)