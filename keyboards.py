from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import texts
from config import OFERTA_URL, PRIVACY_URL
from db.models import Product


def main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🛍 Каталог методичек", callback_data="catalog")
    b.button(text="📦 Мои покупки", callback_data="purchases")
    b.button(text="👤 Мой профиль", callback_data="profile")
    b.button(text="🛠 Техподдержка", callback_data="support")
    b.button(text="ℹ️ О магазине", callback_data="about")
    b.button(text="📄 Документы", callback_data="docs")
    b.adjust(2)  # 2 столбца: 2 + 2 + 1
    return b.as_markup()


def catalog(products: list[Product]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in products:
        b.button(
            text=f"{p.emoji} {p.title} — {texts.price(p.price)}",
            callback_data=f"product:{p.id}",
        )
    b.adjust(1)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu"))
    return b.as_markup()


def product_card(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", callback_data=f"pay:{product_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="catalog")],
        ]
    )


def registration(agreed: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=("✅" if agreed else "❌") + " Я согласен с условиями", callback_data="toggle_consent")
    b.button(text="🚀 Зарегистрироваться" if agreed else "🚫 Зарегистрироваться", callback_data="do_register")
    b.adjust(1)
    return b.as_markup()


def _doc_links() -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton(text="📜 Договор оферты", url=OFERTA_URL)],
        [InlineKeyboardButton(text="🔒 Политика конфиденциальности", url=PRIVACY_URL)],
    ]


def cancel_registration() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel_reg")]]
    )


def docs() -> InlineKeyboardMarkup:
    rows = _doc_links()
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def support() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Написать в поддержку", callback_data="support_write")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
        ]
    )


def support_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="support")]]
    )


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu")]]
    )


def back_to_catalog() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛍 В каталог", callback_data="catalog")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu")],
        ]
    )

def profile() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📦 Мои покупки", callback_data="purchases")
    b.button(text="✏️ Изменить ФИО", callback_data="edit_fio")
    b.button(text="✏️ Изменить Email", callback_data="edit_email")
    b.button(text="⬅️ Назад в меню", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


def cancel_edit() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="profile")]]
    )

def debug_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="👤 Управление пользователями",
            callback_data="god:users",
        )],
        [InlineKeyboardButton(
            text="📦 Управление заказами",
            callback_data="god:orders",
        )],
        [InlineKeyboardButton(
            text="💳 Платежи и доставка",
            callback_data="god:delivery",
        )],
        [InlineKeyboardButton(
            text="🖥 Системные функции",
            callback_data="god:system",
        )],
        [InlineKeyboardButton(
            text="🧪 Тестирование",
            callback_data="god:tests",
        )],
        [InlineKeyboardButton(
            text="⚙️ Общие функции",
            callback_data="god:common",
        )],
        [InlineKeyboardButton(
            text="🔒 Закрыть все активные тикеты",
            callback_data="god:close_tickets",
        )],
    ])


def debug_category(category: str) -> InlineKeyboardMarkup:
    categories = {
        "users": [
            ("🚫 Бан/Разбан", "ban"),
            ("🚫 Ограничить доступ", "delete"),
            ("👑 Выдать/забрать админку", "admin"),
            ("🔄 Сброс регистрации", "reset"),
            ("🔍 Инфо о юзере", "info"),
        ],
        "orders": [
            ("🔁 Отправить заказ заново", "resend"),
            ("📎 Обновить файл товара", "file"),
        ],
        "system": [
            ("📜 Посмотреть логи", "logs"),
            ("📡 Пинг БД", "ping"),
            ("📊 Экспорт CSV", "csv"),
            ("💾 Бэкап БД", "backup"),
        ],
        "tests": [
            ("🧪 Безопасная симуляция PDF", "simulate"),
            ("💥 Crash Test", "crash"),
            ("📄 Тест PDF", "pdf"),
        ],
        "common": [
            ("📢 Рассылка", "broadcast"),
            ("✉️ Написать юзеру", "dm"),
            ("🚧 Режим обслуживания", "maintenance"),
        ],
    }

    builder = InlineKeyboardBuilder()

    for title, action in categories.get(category, []):
        builder.button(text=title, callback_data=f"god:{action}")

    builder.button(
        text="⬅️ Меню разработчика",
        callback_data="god:panel",
    )
    builder.adjust(1)
    return builder.as_markup()