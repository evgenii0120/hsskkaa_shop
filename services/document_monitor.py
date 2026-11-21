import asyncio
import difflib
import hashlib
import html
import logging
import re
from html.parser import HTMLParser

import aiohttp
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from playwright.async_api import async_playwright

from config import OFERTA_URL, PRIVACY_URL
from db.base import SessionMaker
from db.crud import get_setting, set_setting
from db.models import User

logger = logging.getLogger(__name__)
CHECK_INTERVAL_SECONDS = 60 * 60

DOCUMENTS = (
    ("oferta", "договор оферты", OFERTA_URL, "Ознакомиться с договором оферты"),
    ("privacy", "политика конфиденциальности", PRIVACY_URL, "Ознакомиться с политикой конфиденциальности"),
)


class _VisibleTextParser(HTMLParser):
    """Collect visible page text while ignoring scripts and non-content tags."""

    _ignored_tags = {"script", "style", "noscript", "svg", "head", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in self._ignored_tags:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._ignored_tags and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            cleaned = re.sub(r"\s+", " ", data).strip()
            if cleaned:
                self.parts.append(cleaned)


def _extract_text(source: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(source)
    text = " ".join(parser.parts)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 100:
        raise ValueError("Страница вернула слишком мало текста; изменение не засчитывается")
    return text



async def _fetch_text(http: aiohttp.ClientSession, url: str) -> str:
    """Получает текст документа, сохраняя разделение на абзацы и пункты."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        try:
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                )
            )

            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=45000,
            )

            if response is None:
                raise ValueError("Не удалось получить ответ сайта")

            if response.status >= 400:
                raise ValueError(f"Сайт вернул HTTP {response.status}")

            await page.wait_for_timeout(3000)

            source = await page.locator("body").inner_text(timeout=15000)

            # Сохраняем границы абзацев, убирая лишние пробелы.
            lines = []

            for line in source.splitlines():
                line = re.sub(r"\s+", " ", line).strip()

                if line:
                    lines.append(line)
                elif lines and lines[-1] != "":
                    lines.append("")

            while lines and lines[-1] == "":
                lines.pop()

            text = "\n".join(lines)

            if len(text) < 100:
                raise ValueError(
                    "После загрузки страницы получено слишком мало текста"
                )

            return text

        finally:
            await browser.close()



def _split_sections(text: str) -> list[str]:
    """
    Разбивает документ на самостоятельные пункты.

    Поддерживает нумерацию:
    1.
    1.1.
    1.1.1.
    2)
    2.1)
    """

    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in text.splitlines()
        if line.strip()
    ]

    if not lines:
        return []

    # Нумерованный пункт или подпункт.
    section_pattern = re.compile(
        r"^(?:\d+(?:\.\d+)*[.)]?|"
        r"\d+(?:\.\d+)*\s*[.)])\s+\S"
    )

    sections = []
    current_section = []

    for line in lines:
        if section_pattern.match(line) and current_section:
            sections.append(" ".join(current_section))
            current_section = [line]
        else:
            current_section.append(line)

    if current_section:
        sections.append(" ".join(current_section))

    # Если нумерации нет, используем абзацы.
    if len(sections) <= 1:
        paragraphs = [
            re.sub(r"\s+", " ", paragraph).strip()
            for paragraph in re.split(r"\n\s*\n", text)
            if paragraph.strip()
        ]

        if len(paragraphs) > 1:
            return paragraphs

    return sections


def _change_summary(old: str, new: str) -> str:
    """
    Сравнивает документы по пунктам.
    Возвращает полный старый и новый текст изменённых пунктов.
    """

    old_sections = _split_sections(old)
    new_sections = _split_sections(new)

    matcher = difflib.SequenceMatcher(
        a=old_sections,
        b=new_sections,
        autojunk=False,
    )

    removed = []
    added = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():

        if tag in ("delete", "replace"):
            removed.extend(old_sections[i1:i2])

        if tag in ("insert", "replace"):
            added.extend(new_sections[j1:j2])

    lines = []

    if removed:
        lines.append("<b>Удалено/изменено (предыдущая версия):</b>")

        for section in removed:
            lines.append(
                f"<blockquote>{html.escape(section)}</blockquote>"
            )

    if added:
        lines.append("<b>Добавлено/изменено (новая версия):</b>")

        for section in added:
            lines.append(
                f"<blockquote>{html.escape(section)}</blockquote>"
            )

    if not lines:
        lines.append(
            "Текст документа обновлён. "
            "Пожалуйста, ознакомьтесь с актуальной редакцией."
        )

    return "\n".join(lines)


async def _broadcast(bot: Bot, title: str, url: str, button_text: str, old: str, new: str) -> None:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=button_text, url=url)]
    ])
    message = (
        "Всем доброго времени суток! 👋\n\n"
        f"У нас обновился <b>{title}</b>.\n\n"
        f"<b>Изменения:</b>\n{_change_summary(old, new)}\n\n"
        "Ознакомиться подробнее вы можете тут 👇"
    )

    async with SessionMaker() as session:
        result = await session.execute(
            select(User.telegram_id).where(User.is_banned.is_(False))
        )
        telegram_ids = list(result.scalars().all())

    sent = 0
    failed = 0
    for telegram_id in telegram_ids:
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text=message,
                reply_markup=keyboard,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            sent += 1
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            failed += 1
            logger.info("Не удалось отправить уведомление пользователю %s: %s", telegram_id, exc)
        # Small pause to reduce the chance of hitting Telegram rate limits.
        await asyncio.sleep(0.05)
    logger.info("Рассылка обновления документа '%s': успешно %s, ошибок %s", title, sent, failed)


async def _check_document(
    bot: Bot,
    http: aiohttp.ClientSession,
    key: str,
    title: str,
    url: str,
    button_text: str,
) -> None:
    try:
        current_text = await _fetch_text(http, url)
    except Exception:
        logger.exception("Не удалось проверить страницу документа: %s", url)
        return

    current_hash = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
    hash_key = f"document_monitor:{key}:sha256"
    text_key = f"document_monitor:{key}:text"

    async with SessionMaker() as session:
        previous_hash = await get_setting(session, hash_key)
        previous_text = await get_setting(session, text_key)

        # First successful check establishes the baseline without a broadcast.
        if not previous_hash:
            await set_setting(session, hash_key, current_hash)
            await set_setting(session, text_key, current_text)
            await session.commit()
            logger.info("Сохранена исходная версия документа: %s", title)
            return

        if previous_hash == current_hash:
            return

        # Persist the new version first so restarts do not cause duplicate broadcasts.
        await set_setting(session, hash_key, current_hash)
        await set_setting(session, text_key, current_text)
        await session.commit()

    logger.warning("Обнаружено изменение документа: %s", title)
    await _broadcast(bot, title, url, button_text, previous_text, current_text)


async def monitor_documents(bot: Bot) -> None:
    """Establish baseline on startup, then check both pages once per hour."""
    timeout = aiohttp.ClientTimeout(total=35, connect=10)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DocumentChangeMonitor/1.0)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as http:
        while True:
            for key, title, url, button_text in DOCUMENTS:
                await _check_document(bot, http, key, title, url, button_text)
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
