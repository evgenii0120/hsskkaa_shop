import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from config import DEBUG_ADMIN_IDS

STARTED_AT = time.time()
errors: deque[tuple[str, str]] = deque(maxlen=20)  # (время, текст)
_last_sent: dict[str, float] = {}
_DEDUP_SECONDS = 30


async def report(bot: Bot, title: str, details: str) -> None:
    """Сохраняет ошибку в буфер и отправляет её единственному debug-админу."""
    stamp = datetime.now().strftime("%d.%m %H:%M:%S")
    errors.append((stamp, f"{title}\n{details}"))
    if not DEBUG_ADMIN_IDS:
        return
    sig = details[-300:]
    now = time.time()
    if now - _last_sent.get(sig, 0) < _DEDUP_SECONDS:  # анти-спам одинаковых ошибок
        return
    _last_sent[sig] = now
    text = f"🐞 <b>{escape(title)}</b> · {stamp}\n<pre>{escape(details[-3300:])}</pre>"
    for admin_id in DEBUG_ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=text)
        except TelegramAPIError:
            pass
        pass


class TelegramLogHandler(logging.Handler):
    """Пересылает записи ERROR+ из logging (кроме уже обработанных ошибок апдейтов)."""

    def __init__(self, bot: Bot) -> None:
        super().__init__(level=logging.ERROR)
        self.bot = bot

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("aiogram.event"):
            return
        try:
            asyncio.get_running_loop().create_task(
                report(self.bot, f"LOG {record.levelname} · {record.name}", self.format(record))
            )
        except RuntimeError:
            pass