from aiogram.types import User as TgUser
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import ADMIN_IDS
from db.models import Product, User, Order, Settings, PaymentAttempt


async def get_or_create_user(session: AsyncSession, tg: TgUser) -> User:
    # PostgreSQL upsert avoids duplicate-user races from simultaneous /start updates.
    await session.execute(
        pg_insert(User).values(
            telegram_id=tg.id,
            is_admin=tg.id in ADMIN_IDS,
            is_banned=False,
            created_at=datetime.now(timezone.utc),
        ).on_conflict_do_nothing(index_elements=["telegram_id"])
    )
    return (await session.execute(
        select(User).where(User.telegram_id == tg.id)
    )).scalar_one()


async def active_products(session: AsyncSession) -> list[Product]:
    result = await session.execute(
        select(Product)
        .where(Product.is_active.is_(True))
        .order_by(Product.id)
    )
    return list(result.scalars())


async def get_active_product(
    session: AsyncSession, product_id: int
) -> Product | None:
    product = await session.get(Product, product_id)
    return product if product and product.is_active else None


async def count_purchases(session: AsyncSession, user_id: int) -> int:
    return (
        await session.execute(
            select(func.count(func.distinct(PaymentAttempt.order_id)))
            .join(Order, Order.id == PaymentAttempt.order_id)
            .where(Order.user_id == user_id, PaymentAttempt.status == "succeeded")
        )
    ).scalar_one()


async def get_setting(
    session: AsyncSession, key: str, default: str = ""
) -> str:
    result = await session.execute(
        select(Settings).where(Settings.key == key)
    )
    setting = result.scalar_one_or_none()
    return setting.value if setting else default


async def set_setting(
    session: AsyncSession, key: str, value: str
) -> None:
    result = await session.execute(
        select(Settings).where(Settings.key == key)
    )
    setting = result.scalar_one_or_none()

    if setting is None:
        session.add(Settings(key=key, value=value))
    else:
        setting.value = value

    await session.flush()