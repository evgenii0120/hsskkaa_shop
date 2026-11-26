import asyncio
import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand

from config import BOT_TOKEN, DELIVERY_WORKER_COUNT
from db.base import engine, init_db
from handlers import admin, debug, fallback, menu, payment, profile, registration, support
from handlers.admin import debug as god_debug, receipts
from middlewares.db import DbSessionMiddleware
from services.debug import TelegramLogHandler
from services.delivery import delivery_worker
from services.document_monitor import monitor_documents
from services.manual_receipts import receipt_worker
from webhook_server import start_payment_watcher, start_webhook_server


def _create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dp.message.outer_middleware(DbSessionMiddleware())
    dp.callback_query.outer_middleware(DbSessionMiddleware())
    dp.include_routers(
        admin.router,
        receipts.router,
        god_debug.router,
        debug.router,
        menu.router,
        registration.router,
        profile.router,
        payment.router,
        support.router,
        fallback.router,
    )
    return dp


async def _run_bot(dp: Dispatcher) -> None:
    if not BOT_TOKEN:
        raise RuntimeError("В .env не задан BOT_TOKEN.")

    async with AsyncExitStack() as resources:
        resources.push_async_callback(engine.dispose)
        await init_db()

        bot = Bot(
            token=BOT_TOKEN,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                link_preview_is_disabled=True,
            ),
        )
        resources.push_async_callback(bot.session.close)
        root_logger = logging.getLogger()
        telegram_handler = TelegramLogHandler(bot)
        root_logger.addHandler(telegram_handler)
        resources.callback(root_logger.removeHandler, telegram_handler)

        await bot.set_my_commands([
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="menu", description="Главное меню"),
        ])
        await bot.delete_webhook(drop_pending_updates=False)

        webhook_runner = await start_webhook_server(bot)
        if webhook_runner is not None:
            resources.push_async_callback(webhook_runner.cleanup)

        tasks: list[asyncio.Task] = []
        resources.push_async_callback(_stop_background_tasks, tasks)
        tasks.append(asyncio.create_task(_watch_python_files(), name="code-reloader"))
        tasks.append(asyncio.create_task(monitor_documents(bot), name="document-monitor"))
        payment_task = start_payment_watcher(bot)
        if payment_task is not None:
            tasks.append(payment_task)
        tasks.append(asyncio.create_task(
            receipt_worker(bot, notifications=True), name="receipt-notifications",
        ))
        tasks.append(asyncio.create_task(
            receipt_worker(bot, notifications=False), name="receipt-email",
        ))
        for index in range(1, DELIVERY_WORKER_COUNT + 1):
            tasks.append(asyncio.create_task(
                delivery_worker(bot, f"delivery-{index}"), name=f"delivery-worker-{index}",
            ))

        await dp.start_polling(
            bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False,
        )


async def _stop_background_tasks(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    dp = _create_dispatcher()
    restart_delay = 10

    while True:
        try:
            logging.info("Запуск Telegram-бота...")
            await _run_bot(dp)

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
