import asyncio
import logging
import os
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from config import BOT_TOKEN, DELIVERY_WORKER_COUNT
from db.base import engine, init_db
from handlers import admin, debug, fallback, menu, payment, profile, registration, support
from handlers.admin import debug as god_debug
from middlewares.db import DbSessionMiddleware
from services.debug import TelegramLogHandler
from services.delivery import delivery_worker
from services.document_monitor import monitor_documents
from services.receipt_delivery import receipt_worker
from services.yookassa_service import is_configured as yookassa_is_configured
from webhook_server import start_payment_watcher, start_webhook_server


async def _run_bot() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Але утырок, а токен бота кто писать будет?")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(DbSessionMiddleware())
    dp.callback_query.outer_middleware(DbSessionMiddleware())
    dp.include_routers(
        admin.router,
        god_debug.router,
        debug.router,
        menu.router,
        registration.router,
        profile.router,
        payment.router,
        support.router,
        fallback.router,
    )
    logging.getLogger().addHandler(TelegramLogHandler(bot))

    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="menu", description="Главное меню"),
    ])

    await bot.delete_webhook(drop_pending_updates=False)
    reload_task = asyncio.create_task(
        _watch_python_files(),
        name="code-reloader",
    )

    monitor_task = asyncio.create_task(
        monitor_documents(bot),
        name="document-monitor",
    )

    webhook_runner = await start_webhook_server(bot)
    payment_task = start_payment_watcher(bot)
    receipt_task = (asyncio.create_task(receipt_worker(), name="receipt-worker")
                    if yookassa_is_configured() else None)
    workers = [
        asyncio.create_task(delivery_worker(bot, f"delivery-{i + 1}"), name=f"delivery-worker-{i + 1}")
        for i in range(DELIVERY_WORKER_COUNT)
    ]

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in [
            reload_task,
            monitor_task,
            payment_task,
            receipt_task,
            *workers,
        ]:
            if task:
                task.cancel()
        await asyncio.gather(
            *(
                task
                for task in [
                    reload_task,
                    monitor_task,
                    payment_task,
                    receipt_task,
                    *workers,
                ]
                if task
            ),
            return_exceptions=True,
        )
        if webhook_runner:
            await webhook_runner.cleanup()
        await bot.session.close()
        await engine.dispose()

def _get_python_files_snapshot() -> dict:
    project_dir = Path(__file__).resolve().parent

    ignored_dirs = {
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
    }

    snapshot = {}

    for file_path in project_dir.rglob("*.py"):
        if any(part in ignored_dirs for part in file_path.parts):
            continue

        try:
            stat = file_path.stat()

            snapshot[str(file_path)] = (
                stat.st_mtime_ns,
                stat.st_size,
            )

        except OSError:
            continue

    return snapshot


async def _watch_python_files() -> None:
    logger = logging.getLogger(__name__)

    previous_snapshot = _get_python_files_snapshot()

    logger.info("Автоматическое отслеживание изменений кода включено.")

    while True:
        await asyncio.sleep(1)

        current_snapshot = _get_python_files_snapshot()

        if current_snapshot != previous_snapshot:
            logger.info(
                "Обнаружены изменения в Python-файлах. "
                "Перезапуск через 1 секунду..."
            )

            await asyncio.sleep(1)

            logger.info("Выполняется автоматический перезапуск бота...")

            os.execv(
                sys.executable,
                [sys.executable] + sys.argv,
            )

async def main() -> None:
    restart_delay = 10

    while True:
        try:
            logging.info("Запуск Telegram-бота...")
            await _run_bot()

            # Если polling неожиданно завершился без ошибки.
            logging.warning(
                "Работа бота завершилась. "
                "Повторный запуск через %s секунд.",
                restart_delay,
            )

        except asyncio.CancelledError:
            raise

        except KeyboardInterrupt:
            logging.info("Получена команда остановки бота.")
            break

        except Exception:
            logging.exception(
                "Критическая ошибка! "
                "Бот будет перезапущен через %s секунд.",
                restart_delay,
            )

        await asyncio.sleep(restart_delay)


if __name__ == "__main__":
    try:
        asyncio.run(main())
        print ('Бот запущен')
    except KeyboardInterrupt:
        logging.info("Бот остановлен пользователем.")
