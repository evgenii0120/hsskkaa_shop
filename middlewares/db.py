from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from sqlalchemy import select

from config import DEBUG_ADMIN_IDS
from db.base import SessionMaker
from db.crud import get_setting
from db.models import User


class DbSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[
            [TelegramObject, dict[str, Any]], Awaitable[Any]
        ],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with SessionMaker() as session:
            data["session"] = session
            tg = getattr(event, "from_user", None)

            try:
                if tg and tg.id not in DEBUG_ADMIN_IDS:
                    result = await session.execute(
                        select(User).where(User.telegram_id == tg.id)
                    )
                    user = result.scalar_one_or_none()

                    if user and user.is_banned:
                        answer = "🚫 Ваш доступ к магазину ограничен."
                    elif (
                        await get_setting(
                            session, "maintenance_mode", "0"
                        )
                        == "1"
                    ):
                        answer = (
                            "🚧 Магазин на обновлении. "
                            "Попробуйте позже."
                        )
                    else:
                        answer = None

                    if answer:
                        if isinstance(event, Message):
                            await event.answer(answer)
                        elif isinstance(event, CallbackQuery):
                            await event.answer(answer, show_alert=True)

                        await session.commit()
                        return None

                result = await handler(event, data)
                await session.commit()
                return result

            except Exception:
                await session.rollback()
                raise