from aiogram import Router
from aiogram.filters import BaseFilter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Kb,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import ADMIN_IDS, DEBUG_ADMIN_IDS
from db.models import User


class IsAdmin(BaseFilter):
    async def __call__(
        self,
        event: Message | CallbackQuery,
        session: AsyncSession,
    ) -> bool:
        tg = event.from_user

        if tg is None:
            return False

        # Администраторы, указанные в .env
        if tg.id in (ADMIN_IDS | DEBUG_ADMIN_IDS):
            return True

        # Администраторы, выданные через год-дебаг
        result = await session.execute(
            select(User.is_admin).where(User.telegram_id == tg.id)
        )
        return result.scalar_one_or_none() is True


def admin_router() -> Router:
    r = Router()
    r.message.filter(IsAdmin())
    r.callback_query.filter(IsAdmin())
    return r


PANEL_TEXT = "👑 <b>Админ-панель</b>\n\nВыберите раздел 👇"


def panel_kb() -> Kb:
    return Kb(inline_keyboard=[
        [Btn(text="📊 Статистика", callback_data="adm:stats")],
        [Btn(text="💳 Платежи и доставка", callback_data="adm:delivery")],
        [Btn(text="📦 Управление товарами", callback_data="adm:products")],
        [Btn(text="💬 Техподдержка (Тикеты)", callback_data="adm:tickets")],
        [Btn(text="⬅️ Выйти в главное меню", callback_data="menu")],
    ])


def back_kb(
    to: str = "adm:panel",
    text: str = "⬅️ Назад",
) -> Kb:
    return Kb(inline_keyboard=[
        [Btn(text=text, callback_data=to)]
    ])