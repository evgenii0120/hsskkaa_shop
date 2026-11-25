from html import escape
from zoneinfo import ZoneInfo

from aiogram import F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DeliveryAttempt, DeliveryTask, Order, PaymentAttempt, PaymentReceipt, Product, User
from handlers.admin.common import admin_router
from handlers.common import edit_or_answer
from services.delivery import force_retry_task
from services.receipt_delivery import queue_receipt_email

router = admin_router()


def _status(value: str) -> str:
    return {
        "pending": "ожидает", "processing": "в работе", "retry_wait": "повтор позже",
        "delivered": "доставлено", "blocked": "заблокировано", "skipped": "пропущено",
        "creating": "создаётся", "creation_unknown": "результат создания неизвестен",
        "creation_failed": "ошибка создания", "succeeded": "оплачено",
        "canceled": "отменено", "reconciliation_required": "сверка нужна",
    }.get(value, value)


_RECEIPT_STATUS = {
    "waiting": "ожидает проверки", "pending": "в обработке", "registered": "зарегистрирован",
    "canceled": "регистрация отменена",
    "not_found": "предыдущая проверка не получила чек из API",
    "api_empty": "API вернул пустой список чеков",
    "no_payment_receipt": "в ответе API нет чека прихода по этому платежу",
    "no_payment_id": "нет ID платежа",
    "unconfirmed": "API не подтвердил регистрацию",
    "details_unavailable": "зарегистрирован, API не вернул полные реквизиты", "api_error": "ошибка API", "test": "тестовый платёж",
    "mismatch": "платёж не совпадает с заказом", "not_applicable": "чек отсутствует у отменённого платежа",
}
_RECEIPT_EMAIL_STATUS = {
    "queued": "в очереди", "sending": "отправляется", "accepted": "принято SMTP",
    "blocked": "отправка недоступна", "failed": "ошибка отправки", "unknown": "результат неизвестен",
}


async def _dashboard(cb: CallbackQuery, session: AsyncSession) -> None:
    orders = (await session.execute(
        select(Order).order_by(Order.id.desc()).limit(15)
    )).scalars().all()
    lines = ["<b>💳 Платежи и доставка</b>", "Последние 15 заказов:", ""]
    rows = []
    for order in orders:
        payments = (await session.execute(
            select(PaymentAttempt).where(PaymentAttempt.order_id == order.id)
            .order_by(PaymentAttempt.id.desc())
        )).scalars().all()
        tasks = (await session.execute(
            select(DeliveryTask).where(DeliveryTask.order_id == order.id)
        )).scalars().all()
        paid = any(p.status == "succeeded" for p in payments)
        delivered = sum(t.status == "delivered" for t in tasks)
        lines.append(
            f"Заказ <b>#{order.id}</b> · {'оплачен' if paid else 'не подтверждён'} · "
            f"доставлено {delivered}/2 · попыток оплаты {len(payments)}"
        )
        rows.append([InlineKeyboardButton(
            text=f"Заказ #{order.id} — подробности", callback_data=f"adm:dl:{order.id}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="adm:panel")])
    await edit_or_answer(cb, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.in_({"adm:delivery", "god:delivery"}))
async def delivery_dashboard(cb: CallbackQuery, session: AsyncSession) -> None:
    await _dashboard(cb, session)


@router.callback_query(F.data.startswith("adm:dl:") | F.data.startswith("god:dl:"))
async def delivery_order_detail(cb: CallbackQuery, session: AsyncSession) -> None:
    try:
        order_id = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        await cb.answer("Некорректный ID", show_alert=True)
        return
    order = await session.get(Order, order_id)
    if not order:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    user = await session.get(User, order.user_id)
    product = await session.get(Product, order.product_id)
    payments = (await session.execute(
        select(PaymentAttempt).where(PaymentAttempt.order_id == order_id)
        .order_by(PaymentAttempt.id.desc())
    )).scalars().all()
    tasks = (await session.execute(
        select(DeliveryTask).where(DeliveryTask.order_id == order_id)
        .order_by(DeliveryTask.channel)
    )).scalars().all()
    lines = [
        f"<b>Заказ #{order.id}</b>",
        f"Покупатель: {escape(order.customer_fio or (user.fio if user else None) or '—')}",
        f"Email: {escape(order.customer_email or (user.email if user else None) or '—')}",
        f"Товар: {escape(product.title) if product else '—'}",
        f"Сумма: {order.amount} RUB",
        "", "<b>Попытки оплаты:</b>",
    ]
    rows = []
    tracking = {
        item.attempt_id: item
        for item in (await session.scalars(select(PaymentReceipt).where(
            PaymentReceipt.attempt_id.in_([p.id for p in payments])
        ))).all()
    }
    for p in payments:
        lines.append(
            f"#{p.id}: {_status(p.status)} · {p.amount} {p.currency} · "
            f"payment_id={escape(p.payment_id or '—')}"
        )
        if p.last_error:
            lines.append(f"  Ошибка: {escape(p.last_error[:250])}")
        receipt = tracking.get(p.id)
        if receipt is not None:
            fiscal = _RECEIPT_STATUS.get(receipt.status, receipt.status)
            mail = _RECEIPT_EMAIL_STATUS.get(receipt.email_status, "нет отправки ботом")
            provider = escape(receipt.registration_status or "API не вернул")
            lines.append(f"  Чек: {escape(fiscal)} · письмо: {mail}")
            if receipt.checked_at:
                checked = receipt.checked_at.astimezone(ZoneInfo("Europe/Moscow"))
                lines.append(f"  receipt_registration: {provider} · проверено {checked:%d.%m %H:%M} МСК")
            if receipt.next_check_at:
                next_check = receipt.next_check_at.astimezone(ZoneInfo("Europe/Moscow"))
                lines.append(f"  Следующая проверка: {next_check:%d.%m %H:%M} МСК")
            if receipt.email_error:
                lines.append(f"  {escape(receipt.email_error)}")
        if p.payment_id:
            rows.append([InlineKeyboardButton(
                text=f"✉️ Повторить отправку чека · #{p.id}", callback_data=f"adm:receipt:{p.id}"
            )])
    lines.append("\n<b>Каналы доставки:</b>")
    for task in tasks:
        lines.append(f"{task.channel}: <b>{_status(task.status)}</b> · попыток {task.attempts}")
        if task.last_error:
            lines.append(f"  Ошибка: {escape(task.last_error[:250])}")
        history = (await session.execute(
            select(DeliveryAttempt).where(DeliveryAttempt.task_id == task.id)
            .order_by(DeliveryAttempt.attempt_number.desc()).limit(5)
        )).scalars().all()
        for attempt in history:
            lines.append(
                f"  #{attempt.attempt_number}: {attempt.status} · "
                f"{attempt.finished_at or attempt.started_at} · "
                f"{escape(attempt.error_message or attempt.external_message_id or '')[:160]}"
            )
        if task.status in {"blocked", "retry_wait", "pending", "skipped"}:
            rows.append([InlineKeyboardButton(
                text=f"🔁 Повторить {task.channel}", callback_data=f"adm:retry:{task.id}"
            )])
    rows.append([InlineKeyboardButton(text="Обновить", callback_data=f"adm:dl:{order_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ К списку заказов", callback_data="adm:delivery")])
    await edit_or_answer(cb, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("adm:retry:"))
async def retry_delivery(cb: CallbackQuery) -> None:
    try:
        task_id = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        await cb.answer("Некорректный ID", show_alert=True)
        return
    ok = await force_retry_task(task_id)
    await cb.answer(
        "Задание возвращено в очередь" if ok else "Повтор невозможен: проверьте оплату и статус задания",
        show_alert=True,
    )


@router.callback_query(F.data.regexp(r"^adm:receipt:[0-9]+$"))
async def confirm_receipt_resend(cb: CallbackQuery, session: AsyncSession) -> None:
    attempt_id = int(cb.data.rsplit(":", 1)[1])
    attempt = await session.get(PaymentAttempt, attempt_id)
    if not attempt or not attempt.payment_id:
        await cb.answer("У попытки нет ID платежа ЮKassa.", show_alert=True)
        return
    await edit_or_answer(
        cb,
        f"Отправить покупателю фискальные реквизиты чека по заказу #{attempt.order_id}, "
        f"попытка оплаты #{attempt_id}? Бот сначала проверит чек в ЮKassa.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отправить", callback_data=f"adm:receipt-send:{attempt_id}")],
            [InlineKeyboardButton(text="⬅️ К заказу", callback_data=f"adm:dl:{attempt.order_id}")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:receipt-send:[0-9]+$"))
async def resend_receipt(cb: CallbackQuery, session: AsyncSession) -> None:
    attempt_id = int(cb.data.rsplit(":", 1)[1])
    result = await queue_receipt_email(attempt_id)
    attempt = await session.get(PaymentAttempt, attempt_id)
    if attempt:
        await edit_or_answer(cb, result, InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ К заказу", callback_data=f"adm:dl:{attempt.order_id}")
        ]]))
    await cb.answer(result if not attempt else None, show_alert=not bool(attempt))
