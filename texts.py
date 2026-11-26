from html import escape

from db.models import Product
from config import OFERTA_URL, PRIVACY_URL
from datetime import datetime, timedelta, timezone
from html import escape

LINE = "➖➖➖➖➖➖➖➖➖➖"

WELCOME = (
    "👋 <b>Привет! Я - Ирина Вячеславовна, и здесь ты можешь приобрести авторские рабочие тетради.</b>\n\n"
    "📚 Здесь ты найдешь материалы по <b>математике</b> и <b>физике</b> для 5-11 классов.\n"
    "Всё написано простым языком, без «воды», с разбором реальных заданий.\n\n"
    f"{LINE}\n"
    "Выбери нужный раздел 👇"
)

MENU = "🏠 <b>Главное меню</b>\n\nКуда отправимся? 👇"

CATALOG = (
    "🛍 <b>Каталог тетрадей</b>\n\n"
    "Выбери предмет или тему, чтобы посмотреть подробности 👇"
)

MY_PURCHASES = (
    "📦 <b>Мои покупки</b>\n\n"
    "Тут будут появляться купленные файлы.\n"
    "Пока что список пуст. Загляни в каталог, чтобы найти что-то полезное! 👇"
)

ABOUT = (
    "ℹ️ <b>О магазине</b>\n\n"
    "Меня зовут Ирина Вячеславовна, и я создаю эти тетради для вас!\n\n"
    "🎯 <b>Моя цель</b> — сделать обучение понятным и структурированным.\n\n"
    "✔ Только актуальные материалы\n"
    "✔ Понятная теория и разборы\n"
    "✔ Тренировочные задачи с ответами\n\n"
    "Все файлы в электронном виде (PDF) — можно заниматься с телефона или распечатать."
)

DOCS = (
    "📑 <b>Документы магазина</b>\n\n"
    "Мы работаем официально. Перед покупкой, пожалуйста, ознакомься с документами:\n\n"
    "📜 <b>Договор оферты</b>\n"
    "🔒 <b>Политика конфиденциальности</b>"
)

SUPPORT = (
    "🛠 <b>Техподдержка</b>\n\n"
    "Возникли вопросы по оплате, доступу к файлам или самим методичкам?\n\n"
    "Нажми кнопку ниже и напиши нам. Мы обязательно поможем! 👇"
)

SUPPORT_ASK = (
    "✍️ <b>Написать в поддержку</b>\n\n"
    "Опиши свой вопрос одним сообщением. Ты можешь прикрепить фото или скриншот проблемы."
)

SUPPORT_SENT = "✅ Сообщение отправлено! Администратор свяжется с тобой здесь в ближайшее время."

SUPPORT_UNSUPPORTED = "⚠️ Такой тип сообщений не поддерживается. Отправьте текст, фото, видео, голосовое, стикер или гифку."

ADMIN_DENIED = "⛔ Ошибка авторизации: у вас нет доступа к админ-панели."

CATALOG_EMPTY = "🛍 Каталог пока пуст. Загляните позже!"

PRODUCT_NOT_FOUND = "❌ Ой, эта методичка не найдена или снята с продажи."

ASK_FIO = (
    "🧾 <b>Регистрация</b>\n\n"
    "Шаг 1 из 2. Пожалуйста, введи своё <b>ФИО</b>:\n"
    "(оно понадобится для отправки методички и чека)\n\n"
    "<i>Пример: Иванов Иван Иванович</i>"
)

BAD_FIO = (
    "⚠️ <b>Похоже, произошла ошибка.</b>\n\n"
    "Введи фамилию, имя и отчество (если есть), используя только буквы.\n\n"
    "<i>Пример: Иванов Иван Иванович</i>"
)

ASK_EMAIL = (
    "🧾 <b>Регистрация</b>\n\n"
    "Шаг 2 из 2. Введи свой <b>Email</b>:\n"
    "(на него придёт письмо с готовым файлом.)\n\n"
    "<i>Пример: name@example.com</i>"
)

BAD_EMAIL = (
    "⚠️ <b>Кажется, в адресе опечатка.</b>\n\n"
    "Пожалуйста, введи Email в формате: <i>name@example.com</i>"
)

TEXT_ONLY = "✏️ Пожалуйста, отправь ответ обычным текстовым сообщением."

USE_BUTTONS = "👇 Пожалуйста, используй кнопки под сообщением, чтобы я тебя понял."

FALLBACK = "🤔 Я тебя не совсем понял. Давай воспользуемся кнопками меню! 👇"


def price(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


def product_card(p: Product) -> str:
    includes = "\n".join(f"✅ {escape(item)}" for item in p.includes)
    return (
        f"{p.emoji} <b>{escape(p.title)}</b>\n"
        f"<i>{escape(p.subtitle)}</i>\n\n"
        f"{escape(p.description)}\n\n"
        f"<b>Что внутри:</b>\n{includes}\n\n"
        f"<blockquote>"
        f"📎 Формат: {escape(p.file_format)}\n"
        f"💰 Стоимость: <b>{price(p.price)}</b>"
        f"</blockquote>"
    )


def _order_block(p: Product, fio: str, email: str) -> str:
    # Красиво оформленный блок для чека
    return (
        f"<blockquote>"
        f"📦 <b>Товар:</b> {escape(p.title)}\n"
        f"💳 <b>К оплате:</b> {price(p.price)}\n"
        f"👤 <b>ФИО:</b> {escape(fio)}\n"
        f"📧 <b>Email:</b> {escape(email)}"
        f"</blockquote>"
    )


def consent(p: Product, fio: str, email: str, agreed: bool) -> str:
    head = (
        "📑 <b>Обязательное согласие</b>\n\n"
        "Перед оплатой, пожалуйста, ознакомься с нашими документами.\n"
        "Ссылки на них — в кнопках ниже 👇\n\n"
    )
    
    order = _order_block(p, fio, email)
    
    if agreed:
        tail = "\n\n✅ <b>Отлично!</b> Ты согласился с условиями. Жми «Перейти к оплате»."
    else:
        tail = "\n\n⚠️ <i>Чтобы продолжить, нажми кнопку согласия под этим сообщением.</i>"
        
    return head + order + tail


DELIVERY_BEFORE_PAYMENT = (
    "После оплаты подготовим твою методичку и отправим её на указанную почту. "
    "Обычно справляемся быстрее, но на отправку может уйти до 24 часов."
)


def payment_confirmed(order_id: int) -> str:
    return (
        f"✅ Оплата заказа №{order_id} прошла. Готовим методичку!\n\n"
        "Пришлём её на почту вместе с чеком. Обычно отправляем раньше, "
        "но не позднее чем через 24 часа после оплаты."
    )


def payment_already_confirmed(order_id: int) -> str:
    return f"✅ Заказ №{order_id} уже оплачен. Повторять оплату не нужно."


def combined_order_email(order_id: int) -> str:
    return (
            f"Заказ №{order_id} готов!\n\n"
            "Методический материал и чек об оплате приложены к письму.\n\n"
            "Данное сообщение создано автоматически, отвечать на него не нужно."
    )


def redirect(p: Product, fio: str, email: str, order_id: int) -> str:
    return (
        "⏳ <b>Перенаправление на ЮKassa...</b>\n"
        f"{LINE}\n"
        f"Заказ №{order_id}\n\n"
        f"{_order_block(p, fio, email)}\n\n"
        f"{DELIVERY_BEFORE_PAYMENT}"
    )

REGISTRATION = (
    "👋 <b>Привет! Я - Ирина Вячеславовна, и здесь ты можешь приобрести авторские рабочие тетради.</b>\n\n"
    "Чтобы продолжить, нужно пройти быструю регистрацию.\n\n"
    f'Нажимая «Зарегистрироваться», ты подтверждаешь, что ознакомился с <a href="{OFERTA_URL}">Договором оферты</a> '
    f'и <a href="{PRIVACY_URL}">Политикой конфиденциальности</a>, и даёшь согласие на обработку персональных данных.\n\n'
    f"{LINE}\n"
    "Отметь согласие и нажми «Зарегистрироваться» 👇"
)

REGISTERED = "✅ <b>Регистрация завершена!</b>"

SELECT_CONSENT = "Сначала подтвердите согласие"

_MSK = timezone(timedelta(hours=3))


def fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:  # SQLite отдаёт naive-время в UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_MSK).strftime("%d.%m.%Y %H:%M") + " МСК"


def profile(u, purchases: int) -> str:
    consent = f"Получено {fmt_dt(u.consent_given_at)}" if u.consent_given_at else "—"
    return (
        "👤 <b>Мой профиль</b>\n"
        f"{LINE}\n"
        f"👤 <b>ФИО:</b> {escape(u.fio or '—')}\n"
        f"📧 <b>Email:</b> {escape(u.email or '—')}\n"
        f"🆔 <b>Telegram ID:</b> <code>{u.telegram_id}</code>\n"
        f"📅 <b>Дата регистрации:</b> {fmt_dt(u.created_at)}\n"
        f"✅ <b>Согласие с документами:</b> {consent}\n"
        f"📦 <b>Куплено методичек:</b> {purchases}"
    )


EDIT_FIO = (
    "✏️ <b>Изменение ФИО</b>\n\n"
    "Введи новое <b>ФИО</b>:\n\n<i>Пример: Иванов Иван Иванович</i>"
)

EDIT_EMAIL = (
    "✏️ <b>Изменение Email</b>\n\n"
    "Введи новый <b>Email</b>:\n\n<i>Пример: name@example.com</i>"
)

PROFILE_SAVED = "✅ Данные обновлены."

SESSION_EXPIRED = "⏳ Сессия истекла. Пожалуйста, вернись в главное меню и повтори действие."