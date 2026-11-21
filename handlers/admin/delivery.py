from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DeliveryAttempt, DeliveryTask, Order, PaymentAttempt, Product, User
from handlers.admin.common import admin_router
from handlers.common import edit_or_answer
from services.delivery import force_retry_task

router = admin_router()


def _status(value: str) -> str:
    return {
        "pending": "ожидает", "processing": "в работе", "retry_wait": "повтор позже",
        "delivered": "доставлено", "blocked": "заблокировано", "skipped": "пропущено",
        "creating": "создаётся", "creation_unknown": "результат создания неизвестен",
        "creation_failed": "ошибка создания", "succeeded": "оплачено",
        "canceled": "отменено", "reconciliation_required": "сверка нужна",
    }.get(value, value)


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
    for p in payments:
        lines.append(
            f"#{p.id}: {_status(p.status)} · {p.amount} {p.currency} · "
            f"payment_id={escape(p.payment_id or '—')}"
        )
        if p.last_error:
            lines.append(f"  Ошибка: {escape(p.last_error[:250])}")
    lines.append("\n<b>Каналы доставки:</b>")
    rows = []
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
