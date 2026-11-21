from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ADMIN_IDS
from db.models import Ticket

# ticket_id -> [(admin_tg_id, message_id)] — уведомления с кнопкой «Взять в работу»
notified: dict[int, list[tuple[int, int]]] = {}


def extract(m: Message) -> tuple[str, str, str | None] | None:
    """(тип, текст/подпись, file_id) или None, если тип не поддерживается."""
    if m.text:
        return "text", m.text, None
    cap = m.caption or ""
    if m.photo:
        return "photo", cap, m.photo[-1].file_id
    if m.animation:  # раньше document: у гифок заполнены оба поля
        return "animation", cap, m.animation.file_id
    if m.video:
        return "video", cap, m.video.file_id
    if m.video_note:
        return "video_note", "", m.video_note.file_id
    if m.voice:
        return "voice", cap, m.voice.file_id
    if m.audio:
        return "audio", cap, m.audio.file_id
    if m.sticker:
        return "sticker", "", m.sticker.file_id
    if m.document:
        return "document", cap, m.document.file_id
    return None


def preview(mtype: str, content: str, limit: int = 300) -> str:
    text = content[:limit]
    return escape(text) if mtype == "text" else escape(f"[{mtype}] {text}".strip())


def take_kb(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Взять в работу", callback_data=f"tk:take:{ticket_id}")]
    ])


def open_kb(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Открыть тикет", callback_data=f"tk:open:{ticket_id}")]
    ])


async def notify_admins(
    bot: Bot, ticket: Ticket, user_label: str, msg: Message, mtype: str, content: str, is_new: bool
) -> None:
    title = "🔔 <b>Новый тикет</b>" if is_new else "💬 <b>Новое сообщение</b>"
    header = (
        f"{title} №{ticket.id}\n👤 {escape(user_label)}\n\n{preview(mtype, content)}"
    )
    targets = [ticket.admin_id] if ticket.admin_id else list(ADMIN_IDS)
    kb = open_kb(ticket.id) if ticket.admin_id else take_kb(ticket.id)
    for admin_id in targets:
        try:
            if mtype != "text":
                await bot.copy_message(chat_id=admin_id, from_chat_id=msg.chat.id, message_id=msg.message_id)
            sent = await bot.send_message(chat_id=admin_id, text=header, reply_markup=kb)
            if not ticket.admin_id:
                notified.setdefault(ticket.id, []).append((admin_id, sent.message_id))
        except TelegramAPIError:
            pass


async def drop_other_take_buttons(bot: Bot, ticket_id: int, taker_id: int) -> None:
    """У остальных админов кнопка «Взять в работу» пропадает."""
    for admin_id, message_id in notified.pop(ticket_id, []):
        if admin_id == taker_id:
            continue
        try:
            await bot.edit_message_reply_markup(chat_id=admin_id, message_id=message_id, reply_markup=None)
        except TelegramAPIError:
            pass


async def send_reply_to_user(bot: Bot, admin_msg: Message, user_tg_id: int, ticket_id: int) -> bool:
    """Копия контента от имени бота, без служебных заголовков."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Ответить", callback_data="support_write")]
    ])
    try:
        await bot.copy_message(
            chat_id=user_tg_id, from_chat_id=admin_msg.chat.id, message_id=admin_msg.message_id, reply_markup=kb
        )
        return True
    except TelegramAPIError:
        return False