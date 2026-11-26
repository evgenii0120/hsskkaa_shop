from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800, hide_parameters=True)
SessionMaker = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    """Verify migrated schema and seed catalog; never mutates schema on app startup."""
    from db import models  # noqa: F401

    required = set(Base.metadata.tables)
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
        )
        existing = {row[0] for row in result}
    missing = required - existing
    if "shop_schema_migrations" not in existing:
        missing.add("shop_schema_migrations (run migration)")
    if missing:
        raise RuntimeError(
            "В базе отсутствуют таблицы: " + ", ".join(sorted(missing))
            + ". Выполните миграцию командой: python -m db.migrate"
        )
    async with engine.connect() as conn:
        columns = await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'manual_receipts' "
            "AND column_name IN ('combined', 'buyer_notified')"
        ))
        if len(columns.all()) != 2:
            raise RuntimeError("Таблица чеков требует обновления. Выполните python -m db.migrate.")
        revision = await conn.execute(text(
            "SELECT 1 FROM shop_schema_migrations WHERE revision = '20261109_payment_delivery_v1'"
        ))
        if revision.scalar_one_or_none() is None:
            raise RuntimeError("Миграция payment/delivery не применена. Выполните python -m db.migrate")
