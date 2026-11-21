import asyncio
import csv
import io
import shutil
import tempfile
import logging
import os
import time
import traceback
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    CallbackQuery,
    ErrorEvent,
    BufferedInputFile,
    FSInputFile,
)
from sqlalchemy import select, delete, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.engine import make_url

from config import DEBUG_ADMIN_IDS, DATABASE_URL
from db.models import (
    User,
    Product,
    Order,
    Ticket,
    TicketMessage,
    PaymentAttempt,
    DeliveryTask,
    DeliveryAttempt,
)
from db.crud import get_setting, set_setting
from handlers.common import edit_or_answer
from services import debug
from states import DebugStates
import keyboards as kb

router = Router()
ADMIN = F.from_user.id.in_(DEBUG_ADMIN_IDS)


async def find_user(session: AsyncSession, telegram_id: int):
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()

def user_card_keyboard(user: User):
    from aiogram.types import (
        InlineKeyboardButton as Btn,
        InlineKeyboardMarkup as Kb,
    )

    telegram_id = user.telegram_id

    return Kb(inline_keyboard=[
        [
            Btn(
                text="🚫 Разблокировать" if user.is_banned
                else "🚫 Заблокировать",
                callback_data=f"god:user:ban:{telegram_id}",
            )
        ],
        [
            Btn(
                text="👑 Забрать админку" if user.is_admin
                else "👑 Выдать админку",
                callback_data=f"god:user:admin:{telegram_id}",
            )
        ],
        [
            Btn(
                text="✏️ Изменить ФИО",
                callback_data=f"god:user:fio:{telegram_id}",
            ),
            Btn(
                text="📧 Изменить почту",
                callback_data=f"god:user:email:{telegram_id}",
            ),
        ],
        [
            Btn(
                text="🔄 Сбросить регистрацию",
                callback_data=f"god:user:reset:{telegram_id}",
            )
        ],
        [
            Btn(
                text="🗑 Удалить данные пользователя",
                callback_data=f"god:user:delete:{telegram_id}",
            )
        ],
        [
            Btn(
                text="🔄 Обновить информацию",
                callback_data=f"god:user:open:{telegram_id}",
            )
        ],
        [
            Btn(
                text="⬅️ К управлению пользователями",
                callback_data="god:users",
            )
        ],
        [
            Btn(
                text="⬅️ В меню разработчика",
                callback_data="god:panel",
            )
        ],
    ])


async def render_user_card(
    target,
    session: AsyncSession,
    user: User,
):
    orders = list(
        (
            await session.execute(
                select(Order).where(Order.user_id == user.id)
            )
        ).scalars()
    )

    paid_orders = list(
        (
            await session.execute(
                select(Order)
                .join(
                    PaymentAttempt,
                    PaymentAttempt.order_id == Order.id,
                )
                .where(
                    Order.user_id == user.id,
                    PaymentAttempt.status == "succeeded",
                )
                .distinct()
            )
        ).scalars()
    )

    products = []

    for order in paid_orders:
        product = await session.get(Product, order.product_id)

        if product:
            products.append(product.title)

    msg = (
        "👤 <b>Карточка пользователя</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 Telegram ID: <code>{user.telegram_id}</code>\n"
        f"🗄 ID в базе: <code>{user.id}</code>\n\n"
        f"👤 ФИО: {escape(user.fio or 'Не указано')}\n"
        f"📧 Почта: {escape(user.email or 'Не указана')}\n\n"
        f"📅 Создан: {user.created_at}\n"
        f"📋 Согласие: {user.consent_given_at or 'Нет'}\n\n"
        f"👑 Администратор: {'Да' if user.is_admin else 'Нет'}\n"
        f"🚫 Заблокирован: {'Да' if user.is_banned else 'Нет'}\n\n"
        f"📦 Всего заказов: {len(orders)}\n"
        f"✅ Оплаченных заказов: {len(paid_orders)}\n"
        f"🛍 Купленные товары: "
        f"{escape(', '.join(products) or 'Нет')}"
    )

    if isinstance(target, CallbackQuery):
        await edit_or_answer(
            target,
            msg,
            user_card_keyboard(user),
        )
    else:
        await target.answer(
            msg,
            reply_markup=user_card_keyboard(user),
        )
        
async def find_order(session: AsyncSession, order_id: int):
    return await session.get(Order, order_id)


async def find_product(session: AsyncSession, product_id: int):
    return await session.get(Product, product_id)


async def safe_send(bot: Bot, chat_id: int, text: str) -> bool:
    try:
        await bot.send_message(chat_id, text)
        return True
    except Exception:
        logging.exception("Failed to send Telegram message to %s", chat_id)
        return False


async def report_error(bot: Bot, title: str, exc: Exception) -> None:
    await debug.report(
        bot,
        title,
        "".join(traceback.format_exception(exc)),
    )


@router.errors()
async def error_handler(event: ErrorEvent, bot: Bot) -> bool:
    exc = event.exception
    logging.getLogger("aiogram.event").error(
        "Unhandled update error",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    await report_error(bot, "Debug panel error", exc)

    try:
        if event.update.callback_query:
            await event.update.callback_query.answer(
                "⚠️ Произошла ошибка.", show_alert=True
            )
        elif event.update.message:
            await event.update.message.answer("⚠️ Произошла ошибка.")
    except Exception:
        pass

    return True


@router.message(Command("debug"), ADMIN)
async def open_panel(message: Message):
    await message.answer(
        "🐞 <b>Функции разработчика</b>",
        reply_markup=kb.debug_menu(),
    )


@router.callback_query(F.data == "god:panel", ADMIN)
async def panel(callback: CallbackQuery):
    await edit_or_answer(
        callback,
        "🐞 <b>Функции разработчика</b>",
        kb.debug_menu(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("god:"), ADMIN)
async def dispatch(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
):
    action = callback.data.split(":", 1)[1]
    if action.startswith("user:"):
        parts = callback.data.split(":")

        if len(parts) != 4:
            await callback.answer("Некорректная команда.", show_alert=True)
            return

        _, _, operation, telegram_id_raw = parts

        try:
            telegram_id = int(telegram_id_raw)
        except ValueError:
            await callback.answer("Некорректный ID.", show_alert=True)
            return

        if telegram_id in DEBUG_ADMIN_IDS:
            await callback.answer(
                "Нельзя изменять данные разработчика.",
                show_alert=True,
            )
            return

        user = await find_user(session, telegram_id)

        if not user:
            await callback.answer(
                "Пользователь не найден.",
                show_alert=True,
            )
            return

        if operation == "open":
            await render_user_card(callback, session, user)
            await callback.answer()
            return

        if operation == "ban":
            user.is_banned = not user.is_banned

        elif operation == "admin":
            user.is_admin = not user.is_admin

        elif operation == "reset":
            user.fio = None
            user.email = None
            user.consent_given_at = None

        elif operation in {"fio", "email"}:
            await state.clear()
            await state.update_data(
                debug_target_user_id=telegram_id,
            )

            if operation == "fio":
                await state.set_state(DebugStates.user_edit_fio)
                prompt = "Введите новое ФИО пользователя."
            else:
                await state.set_state(DebugStates.user_edit_email)
                prompt = "Введите новый адрес электронной почты."

            await callback.message.answer(prompt)
            await callback.answer()
            return

        elif operation == "delete":
            from aiogram.types import InlineKeyboardButton as Btn
            from aiogram.types import InlineKeyboardMarkup as Kb

            await callback.message.answer(
                "⚠️ <b>Удаление персональных данных</b>\n\n"
                f"Пользователь: <b>{escape(user.fio or 'Без имени')}</b>\n"
                f"Telegram ID: <code>{user.telegram_id}</code>\n"
                f"Email: {escape(user.email or '—')}\n\n"
                "Будут удалены данные профиля и переписки "
                "с поддержкой, а персональные сведения в связанных "
                "записях будут обезличены.\n\n"
                "Финансовые записи, которые необходимо хранить "
                "по закону, могут быть сохранены без персональных "
                "идентификаторов.\n\n"
                "<b>Это действие нельзя отменить.</b>\n\n"
                "Вы действительно хотите продолжить?",
                reply_markup=Kb(inline_keyboard=[
                    [
                        Btn(
                            text="🗑 Да, удалить данные",
                            callback_data=(
                                f"god:user:delete_confirm:{telegram_id}"
                            ),
                        )
                    ],
                    [
                        Btn(
                            text="❌ Отмена",
                            callback_data=f"god:user:open:{telegram_id}",
                        )
                    ],
                ]),
            )

            await callback.answer()
            return

        elif operation == "delete_confirm":
            # Финальное удаление персональных данных.
            await erase_user_personal_data(session, user)

            await session.flush()

            await callback.message.answer(
                "✅ Персональные данные пользователя обезличены.\n\n"
                "Финансовые записи, подлежащие обязательному хранению, "
                "сохранены без прямых персональных идентификаторов."
            )

            await callback.answer("Операция выполнена.", show_alert=True)
            await state.clear()
            return

        else:
            await callback.answer(
                "Неизвестная операция.",
                show_alert=True,
            )
            return

        await session.flush()
        await render_user_card(callback, session, user)
        await callback.answer("Изменения сохранены.")
        return

    if action == "users":
        await state.clear()
        await state.set_state(DebugStates.user_id)

        await callback.message.answer(
            "👤 <b>Управление пользователями</b>\n\n"
            "Введите Telegram ID пользователя."
        )

        await callback.answer()
        return

    if action in {"orders", "system", "tests", "common"}:
        titles = {
            "users": "👤 Управление пользователями",
            "orders": "📦 Управление заказами",
            "system": "🖥 Системные функции",
            "tests": "🧪 Тестирование",
            "common": "⚙️ Общие функции",
        }
        await edit_or_answer(
            callback, titles[action], kb.debug_category(action)
        )
        await callback.answer()
        return

    if action == "maintenance":
        enabled = (
            await get_setting(session, "maintenance_mode", "0")
        ) != "1"
        await set_setting(
            session, "maintenance_mode", "1" if enabled else "0"
        )
        await callback.answer(
            "Режим обслуживания включён" if enabled
            else "Режим обслуживания выключен",
            show_alert=True,
        )
        return

    if action == "logs":
        try:
            with open(
                "bot.log", "r", encoding="utf-8", errors="replace"
            ) as file:
                lines = file.readlines()[-50:]
            body = "".join(lines) or "Лог пуст."
        except OSError:
            body = "Файл bot.log не найден."

        await callback.message.answer_document(
            BufferedInputFile(body.encode("utf-8"), "bot.log")
        )
        await callback.answer()
        return

    if action == "ping":
        started = time.perf_counter()
        await session.execute(text("SELECT 1"))
        elapsed = (time.perf_counter() - started) * 1000
        await callback.message.answer(
            f"📡 БД доступна. Ответ: {elapsed:.2f} мс"
        )
        await callback.answer()
        return

    if action == "csv":
        await export_csv(callback, session)
        await callback.answer()
        return

    if action == "backup":
        await backup_database(callback, session)
        await callback.answer()
        return

    if action == "crash":
        raise Exception("Test Error: принудительный Crash Test")

    if action == "pdf":
        await send_test_pdf(callback)
        await callback.answer()
        return
    if action == "close_tickets":
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        await callback.message.answer(
            "⚠️ <b>Закрыть все активные тикеты?</b>\n\n"
            "Будут закрыты все тикеты со статусом «открыт» "
            "или «в работе». Это действие нельзя отменить.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔒 Да, закрыть все",
                        callback_data="god:close_tickets_confirm",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data="god:panel",
                    )
                ],
            ]),
        )
        await callback.answer()
        return

    if action == "simulate":
        await send_test_pdf(callback)
        await callback.message.answer(
            "🧪 Безопасная симуляция завершена. Реальный платёж, заказ и отправка покупателю не создавались."
        )
        await callback.answer()
        return

    if action == "status":
        await callback.answer(
            "Ручное изменение статусов отключено. Используйте раздел «Платежи и доставка».",
            show_alert=True,
        )
        return

    if action == "close_tickets_confirm":
        result = await session.execute(
            update(Ticket)
            .where(Ticket.status.in_(("open", "in_progress")))
            .values(status="closed")
        )
        await session.commit()

        count = result.rowcount or 0
        await callback.message.answer(
            f"✅ Закрытие завершено.\n"
            f"Закрыто тикетов: <b>{count}</b>."
        )
        await callback.answer()
        return

    prompts = {
        "resend": (
            DebugStates.order_id,
            "Введите ID заказа для повторной выдачи.",
        ),
        "file": (
            DebugStates.product_id,
            "Введите ID товара.",
        ),
        "broadcast": (
            DebugStates.broadcast,
            "Отправьте текст рассылки.",
        ),
        "dm": (
            DebugStates.dm_user_id,
            "Введите Telegram ID получателя.",
        ),
    }

    if action not in prompts:
        await callback.answer("Неизвестная команда.", show_alert=True)
        return

    field, prompt = prompts[action]
    await state.clear()
    await state.update_data(debug_action=action)
    await state.set_state(field)
    await callback.message.answer(prompt)
    await callback.answer()


async def erase_user_personal_data(
    session: AsyncSession,
    user: User,
) -> None:
    """
    Удаляет или обезличивает персональные данные пользователя.

    Финансовые записи сохраняются для соблюдения требований
    бухгалтерского и налогового учёта.
    """

    # Защита от удаления разработчика.
    if user.telegram_id in DEBUG_ADMIN_IDS:
        raise ValueError(
            "Нельзя удалить данные разработчика."
        )

    # Получаем все заказы пользователя.
    orders = list(
        (
            await session.execute(
                select(Order).where(Order.user_id == user.id)
            )
        ).scalars()
    )

    order_ids = [order.id for order in orders]

    # Удаляем обращения в поддержку.
    # Сообщения тикетов удаляются каскадно.
    await session.execute(
        delete(Ticket).where(Ticket.user_id == user.id)
    )

    # Удаляем персональные снимки из заказов.
    for order in orders:
        order.customer_telegram_id = None
        order.customer_fio = None
        order.customer_email = None

    if order_ids:
        # Удаляем потенциальные персональные данные
        # из записей платёжных попыток.
        attempts = list(
            (
                await session.execute(
                    select(PaymentAttempt).where(
                        PaymentAttempt.order_id.in_(order_ids)
                    )
                )
            ).scalars()
        )

        for attempt in attempts:
            attempt.request_payload = None
            attempt.provider_metadata = None
            attempt.last_error = None

        # Удаляем возможные персональные сведения
        # из истории доставки.
        delivery_tasks = list(
            (
                await session.execute(
                    select(DeliveryTask).where(
                        DeliveryTask.order_id.in_(order_ids)
                    )
                )
            ).scalars()
        )

        task_ids = [task.id for task in delivery_tasks]

        for task in delivery_tasks:
            task.last_error = None

        if task_ids:
            delivery_attempts = list(
                (
                    await session.execute(
                        select(DeliveryAttempt).where(
                            DeliveryAttempt.task_id.in_(task_ids)
                        )
                    )
                ).scalars()
            )

            for attempt in delivery_attempts:
                attempt.error_message = None
                attempt.result_metadata = None

    # Обезличиваем профиль пользователя.
    user.fio = None
    user.email = None
    user.consent_given_at = None

    # Вместо настоящего Telegram ID оставляем
    # технический отрицательный идентификатор.
    # Это сохраняет целостность связей с заказами.
    user.telegram_id = -abs(user.id)

    user.is_admin = False
    user.is_banned = True

    await session.flush()
    
@router.message(DebugStates.user_id, ADMIN)
async def process_user_id(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
):
    try:
        telegram_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    if telegram_id in DEBUG_ADMIN_IDS:
        await message.answer(
            "Нельзя изменять данные разработчика."
        )
        await state.clear()
        return

    user = await find_user(session, telegram_id)

    if not user:
        await message.answer(
            "Пользователь не найден в базе данных."
        )
        await state.clear()
        return

    await state.update_data(
        debug_target_user_id=telegram_id,
    )

    await state.set_state(DebugStates.user_selected)

    await render_user_card(message, session, user)

@router.message(DebugStates.user_edit_fio, ADMIN)
async def process_user_edit_fio(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
):
    fio = (message.text or "").strip()

    if not fio:
        await message.answer("ФИО не может быть пустым.")
        return

    if len(fio) > 255:
        await message.answer(
            "ФИО слишком длинное. Максимум 255 символов."
        )
        return

    data = await state.get_data()
    telegram_id = data.get("debug_target_user_id")

    user = await find_user(session, telegram_id)

    if not user:
        await message.answer("Пользователь не найден.")
        await state.clear()
        return

    user.fio = fio

    await session.flush()
    await state.set_state(DebugStates.user_selected)

    await message.answer("✅ ФИО успешно изменено.")

    await render_user_card(message, session, user)


@router.message(DebugStates.user_edit_email, ADMIN)
async def process_user_edit_email(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
):
    email = (message.text or "").strip().lower()

    if (
        len(email) > 255
        or "@" not in email
        or "." not in email.split("@")[-1]
        or " " in email
    ):
        await message.answer(
            "Введите корректный адрес электронной почты."
        )
        return

    data = await state.get_data()
    telegram_id = data.get("debug_target_user_id")

    user = await find_user(session, telegram_id)

    if not user:
        await message.answer("Пользователь не найден.")
        await state.clear()
        return

    user.email = email

    await session.flush()
    await state.set_state(DebugStates.user_selected)

    await message.answer("✅ Почта успешно изменена.")

    await render_user_card(message, session, user)


async def show_user_info(message, session, user):
    orders = list(
        (
            await session.execute(
                select(Order).join(
                    PaymentAttempt, PaymentAttempt.order_id == Order.id
                ).where(
                    Order.user_id == user.id,
                    PaymentAttempt.status == "succeeded",
                ).distinct()
            )
        ).scalars()
    )

    products = []
    for order in orders:
        product = await session.get(Product, order.product_id)
        if product:
            products.append(product.title)

    await message.answer(
        f"<b>Пользователь</b>\n"
        f"Telegram ID: <code>{user.telegram_id}</code>\n"
        f"ФИО: {escape(user.fio or '—')}\n"
        f"Email: {escape(user.email or '—')}\n"
        f"Дата регистрации: {user.created_at}\n"
        f"Согласие: {user.consent_given_at or 'нет'}\n"
        f"Админ: {'да' if user.is_admin else 'нет'}\n"
        f"Бан: {'да' if user.is_banned else 'нет'}\n"
        f"Купленные товары: {escape(', '.join(products) or 'нет')}"
    )


@router.message(DebugStates.order_id, ADMIN)
async def process_order_id(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
):
    try:
        order_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("ID заказа должен быть числом.")
        return

    order = await find_order(session, order_id)
    if not order:
        await message.answer("Заказ не найден.")
        await state.clear()
        return

    action = (await state.get_data()).get("debug_action")

    if action == "resend":
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        await message.answer(
            f"Для заказа #{order.id} откройте управление отдельными каналами доставки. "
            "Повтор доступен только при подтверждённой оплате.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Открыть платежи и доставку", callback_data=f"adm:dl:{order.id}")
            ]]),
        )
        await state.clear()
        return

    await state.clear()


@router.message(DebugStates.order_status, ADMIN)
async def process_order_status(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
):
    await message.answer(
        "Ручное изменение статуса отключено: оплата определяется PaymentAttempt, "
        "доставка — DeliveryTask. Используйте панель платежей и доставки."
    )
    await state.clear()


@router.message(DebugStates.product_id, ADMIN)
async def process_product_id(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
):
    try:
        product_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("ID товара должен быть числом.")
        return

    product = await find_product(session, product_id)
    if not product:
        await message.answer("Товар не найден.")
        await state.clear()
        return

    await state.update_data(product_id=product_id)
    await state.set_state(DebugStates.product_file)
    await message.answer("Пришлите новый PDF документом.")


@router.message(DebugStates.product_file, ADMIN, F.document)
async def process_product_file(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
):
    document = message.document

    if not document.file_name or not document.file_name.lower().endswith(".pdf"):
        await message.answer("Нужен файл с расширением .pdf.")
        return

    product_id = (await state.get_data()).get("product_id")
    product = await session.get(Product, product_id)

    if product:
        product.file_id = document.file_id
        product.file_format = "PDF"
        await message.answer("✅ Файл товара обновлён.")
    else:
        await message.answer("Товар не найден.")

    await state.clear()


@router.message(DebugStates.broadcast, ADMIN)
async def process_broadcast(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
):
    if not message.text:
        await message.answer("Рассылка поддерживает текстовые сообщения.")
        return

    user_ids = list(
        (
            await session.execute(
                select(User.telegram_id).where(
                    User.is_banned.is_(False)
                )
            )
        ).scalars()
    )

    sent = failed = 0
    for telegram_id in user_ids:
        if await safe_send(bot, telegram_id, message.text):
            sent += 1
        else:
            failed += 1

        await asyncio.sleep(0.04)

    await message.answer(
        f"Рассылка завершена.\nОтправлено: {sent}\nОшибок: {failed}"
    )
    await state.clear()


@router.message(DebugStates.dm_user_id, ADMIN)
async def process_dm_id(message: Message, state: FSMContext):
    try:
        telegram_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Telegram ID должен быть числом.")
        return

    await state.update_data(dm_target=telegram_id)
    await state.set_state(DebugStates.dm_text)
    await message.answer("Введите текст сообщения.")


@router.message(DebugStates.dm_text, ADMIN)
async def process_dm_text(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    target = (await state.get_data()).get("dm_target")

    if not message.text:
        await message.answer("Введите текстовое сообщение.")
        return

    success = await safe_send(bot, target, message.text)
    await message.answer(
        "Сообщение отправлено." if success
        else "Не удалось отправить сообщение."
    )
    await state.clear()


async def export_csv(callback: CallbackQuery, session: AsyncSession):
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "table", "id", "telegram_id", "fio", "email",
        "created_at", "is_admin", "is_banned",
        "order_id", "user_id", "product_id", "amount", "status",
    ])

    users = list((await session.execute(select(User))).scalars())
    orders = list((await session.execute(select(Order))).scalars())

    for user in users:
        writer.writerow([
            "users", user.id, user.telegram_id, user.fio,
            user.email, user.created_at, user.is_admin,
            user.is_banned, "", "", "", "", "",
        ])

    for order in orders:
        writer.writerow([
            "orders", "", "", "", "", order.created_at,
            "", "", order.id, order.user_id, order.product_id,
            order.amount, order.status,
        ])

    await callback.message.answer_document(
        BufferedInputFile(
            output.getvalue().encode("utf-8-sig"),
            filename="users_orders.csv",
        )
    )


async def backup_database(
    callback: CallbackQuery,
    session: AsyncSession,
):
    """Create a real PostgreSQL custom-format backup with pg_dump."""
    executable = shutil.which("pg_dump")
    if not executable:
        await callback.message.answer(
            "pg_dump не найден на сервере. Создайте резервную копию перед миграцией командой из INSTALL.md."
        )
        return

    url = make_url(DATABASE_URL)
    if not url.drivername.startswith("postgresql"):
        await callback.message.answer("Резервное копирование этой конфигурации поддерживает PostgreSQL.")
        return

    fd, backup_path = tempfile.mkstemp(prefix="shop-backup-", suffix=".dump")
    os.close(fd)
    env = os.environ.copy()
    if url.password is not None:
        env["PGPASSWORD"] = url.password
    if url.query.get("sslmode"):
        env["PGSSLMODE"] = str(url.query["sslmode"])

    args = [executable, "--format=custom", "--no-owner", "--no-acl",
            "--host", str(url.host or "127.0.0.1"),
            "--port", str(url.port or 5432),
            "--username", str(url.username or ""),
            "--dbname", str(url.database or ""),
            "--file", backup_path]
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=env
        )
        _stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[-1500:]
            await callback.message.answer(
                "pg_dump завершился с ошибкой.\n<pre>" + escape(detail) + "</pre>"
            )
            return
        if os.path.getsize(backup_path) == 0:
            await callback.message.answer("pg_dump создал пустой файл; резервная копия не отправлена.")
            return
        await callback.message.answer_document(
            FSInputFile(backup_path, filename="hsskkaa_shop_backup.dump"),
            caption="Резервная копия PostgreSQL. Храните её отдельно от сервера.",
        )
    finally:
        try:
            os.unlink(backup_path)
        except OSError:
            logging.warning("Could not remove temporary backup %s", backup_path)


async def send_test_pdf(callback: CallbackQuery):
    """Generate and send a watermarked sample PDF only to the invoking admin."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from services.pdf_service import add_watermark

        source = io.BytesIO()
        pdf = canvas.Canvas(source, pagesize=A4)
        pdf.setFont("Helvetica-Bold", 18)
        pdf.drawString(60, 780, "SAFE DELIVERY TEST")
        pdf.setFont("Helvetica", 11)
        pdf.drawString(60, 750, "This document is generated locally for administrator testing.")
        pdf.save()
        source.seek(0)
        result = await asyncio.to_thread(
            add_watermark, source, "Тестовый получатель",
            "test@example.invalid", "TEST-ONLY",
        )
        await callback.message.answer_document(
            BufferedInputFile(result.getvalue(), "delivery_test_watermarked.pdf"),
            caption="Тестовый PDF с водяным знаком. Оплата и доставка покупателю не запускались.",
        )
    except Exception as exc:
        logging.exception("Test PDF generation failed")
        await callback.message.answer(
            f"Не удалось создать тестовый PDF: {escape(type(exc).__name__)}: {escape(str(exc)[:500])}"
        )
