from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DeliveryAttempt, DeliveryTask, ManualReceipt, Order, PaymentAttempt, Product, User
from config import DEBUG_ADMIN_IDS
from handlers.common import edit_or_answer
from services.delivery import force_retry_task
from services.manual_receipts import STATUS_LABELS

router = Router()
router.callback_query.filter(F.from_user.id.in_(DEBUG_ADMIN_IDS))
PAGE_SIZE = 15


def _status(value: str) -> str:
    return {
        "pending": "ожидает", "processing": "в работе", "retry_wait": "повтор позже",
        "delivered": "доставлено", "blocked": "заблокировано", "skipped": "пропущено",
        "creating": "создаётся", "creation_unknown": "результат создания неизвестен",
        "creation_failed": "ошибка создания", "succeeded": "оплачено",
        "canceled": "отменено", "reconciliation_required": "сверка нужна",
    }.get(value, value)


@router.callback_query(
    F.data.in_({"adm:delivery", "god:delivery"}) | F.data.regexp(r"^god:delivery:[0-9]{1,10}$")
)
async def delivery_dashboard(cb: CallbackQuery, session: AsyncSession) -> None:
    requested_page = int(cb.data.rsplit(":", 1)[-1]) if cb.data.count(":") == 2 else 0
    total = await session.scalar(select(func.count()).select_from(Order))
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(requested_page, pages - 1)
    orders = (await session.scalars(
        select(Order.id).order_by(Order.created_at.desc(), Order.id.desc())
        .offset(page * PAGE_SIZE).limit(PAGE_SIZE)
    )).all()

    payments = {
        order_id: (count, paid)
        for order_id, count, paid in (await session.execute(
            select(
                PaymentAttempt.order_id, func.count(),
                func.max(case((PaymentAttempt.status == "succeeded", 1), else_=0)),
            ).where(PaymentAttempt.order_id.in_(orders)).group_by(PaymentAttempt.order_id)
        )).all()
    }
    delivered = dict((await session.execute(
        select(DeliveryTask.order_id, func.count()).where(
            DeliveryTask.order_id.in_(orders), DeliveryTask.status == "delivered",
        ).group_by(DeliveryTask.order_id)
    )).all())

    combined_sent = set((await session.scalars(
        select(PaymentAttempt.order_id).join(ManualReceipt, ManualReceipt.attempt_id == PaymentAttempt.id)
        .where(PaymentAttempt.order_id.in_(orders), ManualReceipt.combined, ManualReceipt.status == "sent")
    )).all())
    lines = ["<b>💳 Платежи и доставка</b>", f"Страница {page + 1} из {pages}", ""]
    rows = []
    if not orders:
        lines.append("Заказов пока нет.")
    for order_id in orders:
        count, paid = payments.get(order_id, (0, False))
        lines.append(
            f"Заказ <b>#{order_id}</b> · {'оплачен' if paid else 'не подтверждён'} · "
            f"доставлено {min(2, delivered.get(order_id, 0) + (order_id in combined_sent))}/2 · попыток оплаты {count}"
        )
        rows.append([InlineKeyboardButton(
            text=f"Заказ #{order_id} — подробности", callback_data=f"god:dl:{page}:{order_id}"
        )])

    arrows = []
    if page > 0:
        arrows.append(InlineKeyboardButton(text="⬅️", callback_data=f"god:delivery:{page - 1}"))
    if page + 1 < pages:
        arrows.append(InlineKeyboardButton(text="➡️", callback_data=f"god:delivery:{page + 1}"))
    if arrows:
        rows.append(arrows)
    rows.append([InlineKeyboardButton(text="⬅️ В меню /debug", callback_data="god:panel")])
    await edit_or_answer(cb, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^(adm:dl|god:dl):([0-9]{1,10}:)?[0-9]{1,10}$"))
async def delivery_order_detail(cb: CallbackQuery, session: AsyncSession) -> None:
    parts = cb.data.split(":")
    order_id = int(parts[-1])
    page = int(parts[-2]) if len(parts) == 4 else 0
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
    receipt_statuses = dict((await session.execute(
        select(ManualReceipt.attempt_id, ManualReceipt.status).where(
            ManualReceipt.attempt_id.in_([payment.id for payment in payments])
        )
    )).all())
    for p in payments:
        lines.append(
            f"#{p.id}: {_status(p.status)} · {p.amount} {p.currency} · "
            f"payment_id={escape(p.payment_id or '—')}"
        )
        if p.last_error:
            lines.append(f"  Ошибка: {escape(p.last_error[:250])}")
        if p.id in receipt_statuses:
            lines.append(f"  Чек и отправка: {STATUS_LABELS[receipt_statuses[p.id]]}")
            rows.append([InlineKeyboardButton(
                text=f"Чек по платежу #{p.id}", callback_data=f"receipts:open:{p.id}",
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
                text=f"🔁 Повторить {task.channel}", callback_data=f"god:dl-retry:{task.id}"
            )])
    rows.append([InlineKeyboardButton(text="Обновить", callback_data=f"god:dl:{page}:{order_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ К списку заказов", callback_data=f"god:delivery:{page}")])
    await edit_or_answer(cb, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^(adm:retry|god:dl-retry):[0-9]{1,10}$"))
async def retry_delivery(cb: CallbackQuery) -> None:
    task_id = int(cb.data.rsplit(":", 1)[1])
    ok = await force_retry_task(task_id)
    await cb.answer(
        "Задание возвращено в очередь" if ok else "Повтор невозможен: проверьте оплату и статус задания",
        show_alert=True,
    )
