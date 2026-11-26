import asyncio
import hashlib
import io
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pypdf import PdfReader
from sqlalchemy import exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

import texts
from config import DEBUG_ADMIN_IDS
from db.base import SessionMaker
from db.models import DeliveryTask, ManualReceipt, Order, PaymentAttempt, Product
from services.delivery import PermanentDeliveryError, build_delivery_pdf, load_delivery_data, enqueue_delivery
from services.email_service import SMTPDeliveryError, send_email
from services.payment_rules import delivery_retry_delay

logger = logging.getLogger(__name__)
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
SEND_TIMEOUT = 120
STATUS_LABELS = {
    "waiting": "нужно загрузить чек",
    "ready": "можно отправить заказ",
    "queued": "письмо в очереди",
    "sending": "письмо отправляется",
    "retry_wait": "отправка повторится",
    "failed": "письмо не отправлено",
    "unknown": "результат отправки неизвестен",
    "sent": "чек принят почтовым сервером",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def enqueue_receipt(session, attempt, order, payment, *, notify: bool, combined: bool = False) -> None:
    if attempt.amount <= 0:
        return
    description = (attempt.request_payload or {}).get("description") or payment.description or ""
    title = description.removeprefix(f"Заказ №{order.id}: ").strip()
    product = await session.get(Product, order.product_id) if not title or len(description) >= 128 else None
    if product:
        title = product.title
    paid_at = getattr(payment, "captured_at", None)
    if isinstance(paid_at, str):
        try:
            paid_at = datetime.fromisoformat(paid_at)
        except ValueError:
            paid_at = None
    if paid_at and paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=timezone.utc)
    await session.execute(insert(ManualReceipt).values(
        attempt_id=attempt.id,
        combined=combined,
        title=(title or "Методический материал")[:255],
        email=order.customer_email,
        paid_at=paid_at or attempt.paid_at,
        status="waiting",
        notify_after=_now() if notify else None,
    ).on_conflict_do_nothing(index_elements=[ManualReceipt.attempt_id]))


def _validate_pdf(pdf: bytes) -> None:
    if not pdf.startswith(b"%PDF-") or len(pdf) > MAX_RECEIPT_BYTES:
        raise ValueError("Пришлите PDF размером до 8 МБ.")
    try:
        reader = PdfReader(io.BytesIO(pdf))
        readable = not reader.is_encrypted and bool(reader.pages)
    except Exception as exc:
        raise ValueError("PDF не открывается. Сохраните чек заново и пришлите файл.") from exc
    if not readable:
        raise ValueError("В PDF нет страниц или он защищён паролем.")


async def store_receipt_pdf(attempt_id: int, pdf: bytes, admin_id: int) -> bool:
    await asyncio.to_thread(_validate_pdf, pdf)
    fingerprint = hashlib.sha256(pdf).hexdigest()
    try:
        async with SessionMaker.begin() as session:
            receipt = await session.get(ManualReceipt, attempt_id, with_for_update=True)
            if receipt is None or receipt.status != "waiting":
                raise ValueError("Для этого платежа уже загрузили чек или закрыли задачу.")
            if not receipt.email:
                raise ValueError("У заказа нет сохранённого email покупателя.")
            receipt.pdf = pdf
            receipt.pdf_hash = fingerprint
            receipt.uploaded_by = admin_id
            receipt.uploaded_at = _now()
            receipt.status = "ready" if receipt.combined else "queued"
            receipt.send_after = None if receipt.combined else _now()
            needs_confirmation = receipt.combined
    except IntegrityError as exc:
        raise ValueError("Этот PDF уже прикреплён к другому платежу.") from exc
    return needs_confirmation


async def queue_receipt_retry(attempt_id: int) -> None:
    async with SessionMaker.begin() as session:
        receipt = await session.get(ManualReceipt, attempt_id, with_for_update=True)
        if receipt is None or receipt.status not in {"ready", "failed", "unknown"}:
            raise ValueError("Повтор сейчас недоступен. Обновите карточку.")
        receipt.status = "queued"
        receipt.send_attempts = 0
        receipt.send_after = _now()
        receipt.error = None


async def _notify_one(bot: Bot) -> None:
    async with SessionMaker.begin() as session:
        row = (await session.execute(
            select(ManualReceipt, Order.id, PaymentAttempt.amount, Order.customer_telegram_id)
            .join(PaymentAttempt, PaymentAttempt.id == ManualReceipt.attempt_id)
            .join(Order, Order.id == PaymentAttempt.order_id)
            .where(ManualReceipt.notify_after <= _now(), ManualReceipt.status != "sent")
            .order_by(ManualReceipt.notify_after, ManualReceipt.attempt_id).limit(1)
            .with_for_update(of=ManualReceipt, skip_locked=True)
        )).one_or_none()
        if not row:
            return
        receipt, order_id, amount, buyer_id = row
        attempt_id = receipt.attempt_id
        notify_buyer = receipt.combined and not receipt.buyer_notified and buyer_id
        remaining = DEBUG_ADMIN_IDS - set(receipt.notified_ids) if receipt.status == "waiting" else set()
        receipt.notify_after = _now() + timedelta(minutes=3)
    buyer_notified = False
    if notify_buyer:
        try:
            await asyncio.wait_for(bot.send_message(buyer_id, texts.payment_confirmed(order_id)), timeout=15)
            buyer_notified = True
        except (TelegramAPIError, TimeoutError):
            logger.warning("Не удалось сообщить покупателю об оплате заказа №%s", order_id)

    sent = set()
    for admin_id in sorted(remaining):
        try:
            await asyncio.wait_for(bot.send_message(
                admin_id, f"Заказ №{order_id} оплачен ({amount:g} ₽). Нужно оформить чек в «Моём налоге».",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Открыть платёж", callback_data=f"receipts:open:{attempt_id}")
                ]]),
            ), timeout=15)
            sent.add(admin_id)
        except (TelegramAPIError, TimeoutError):
            logger.warning("Не удалось уведомить разработчика %s об оплате №%s", admin_id, attempt_id)
    async with SessionMaker.begin() as session:
        receipt = await session.get(ManualReceipt, attempt_id, with_for_update=True)
        delivered = set(receipt.notified_ids) | sent
        receipt.notified_ids = sorted(delivered)
        if buyer_notified:
            receipt.buyer_notified = True
        admin_done = DEBUG_ADMIN_IDS <= delivered or receipt.status != "waiting"
        if admin_done and (not notify_buyer or receipt.buyer_notified or receipt.status == "sent"):
            receipt.notify_after = None


async def _claim_email():
    async with SessionMaker.begin() as session:
        guide_in_progress = exists(select(DeliveryTask.id).where(
            DeliveryTask.order_id == PaymentAttempt.order_id,
            DeliveryTask.channel == "email",
            DeliveryTask.status.in_(("pending", "processing", "retry_wait")),
        ))
        row = (await session.execute(
            select(ManualReceipt, PaymentAttempt.order_id, ManualReceipt.pdf)
            .join(PaymentAttempt, PaymentAttempt.id == ManualReceipt.attempt_id)
            .where(
                ManualReceipt.status.in_(("queued", "retry_wait")),
                ManualReceipt.send_after <= _now(),
                or_(
                    ManualReceipt.combined,
                    ~guide_in_progress,
                    ManualReceipt.uploaded_at <= _now() - timedelta(minutes=5),
                ),
            ).order_by(ManualReceipt.send_after, ManualReceipt.attempt_id)
            .limit(1).with_for_update(of=ManualReceipt, skip_locked=True)
        )).one_or_none()
        if row is None:
            return None
        receipt, order_id, pdf = row
        if not receipt.email or not pdf:
            receipt.status = "failed"
            receipt.error = "У платежа нет адреса покупателя или PDF чека."
            return None
        receipt.status = "sending"
        receipt.send_attempts += 1
        receipt.send_started_at = _now()
        return (
            receipt.attempt_id, order_id, receipt.email, pdf,
            receipt.send_started_at, receipt.send_attempts, receipt.combined,
        )


_SMTP_STEPS = {
    "configuration": "настройки почты",
    "connect": "подключение к почтовому серверу",
    "authentication": "вход на почтовый сервер",
    "message": "подготовка письма",
    "send": "передача письма",
}


async def _send_order_email(bot: Bot, order_id: int, email: str, receipt_pdf: bytes, message_id: str) -> None:
    data = await load_delivery_data(order_id)
    data["email"] = email
    try:
        guide_pdf = await asyncio.wait_for(build_delivery_pdf(bot, data), timeout=90)
    except TimeoutError as exc:
        raise PermanentDeliveryError("Слишком долго готовится PDF. Попробуйте отправить заказ ещё раз.") from exc
    await asyncio.wait_for(send_email(
        email=email,
        subject=f"Методичка и чек по заказу №{order_id}",
        text=texts.combined_order_email(order_id),
        attachment=guide_pdf,
        additional_pdf=(f"receipt_{order_id}.pdf", receipt_pdf),
        order_id=order_id,
        message_id=message_id,
    ), timeout=SEND_TIMEOUT)


async def _send_one(bot: Bot) -> None:
    claim = await _claim_email()
    if claim is None:
        return
    attempt_id, order_id, email, pdf, started_at, attempts, combined = claim
    status, error, next_attempt = "sent", None, None
    try:
        message_id = f"<receipt-{attempt_id}-{uuid4().hex}@hsskkaa.ru>"
        if combined:
            await _send_order_email(bot, order_id, email, pdf, message_id)
        else:
            await asyncio.wait_for(send_email(
                email=email,
                subject=f"Чек по заказу №{order_id}",
                text=f"Здравствуйте! Во вложении чек по заказу №{order_id}.\n\n"
                     "Данное сообщение создано автоматически, отвечать на него не нужно.",
                attachment=pdf, filename=f"receipt_{order_id}.pdf", order_id=order_id,
                message_id=message_id,
            ), timeout=SEND_TIMEOUT)
    except PermanentDeliveryError as exc:
        status, error = "failed", f"Не удалось подготовить методичку: {exc}"
    except SMTPDeliveryError as exc:
        stage = _SMTP_STEPS.get(exc.stage, "отправка письма")
        error = f"Ошибка почты. Этап: {stage}."
        if exc.ambiguous:
            status = "unknown"
            error = "Почтовый сервер мог принять письмо. Перед повтором проверьте доставку."
        elif exc.permanent or attempts >= 5:
            status = "failed"
        else:
            status = "retry_wait"
            next_attempt = _now() + delivery_retry_delay(attempts)
    except TimeoutError:
        status, error = "unknown", "Почтовый сервер не ответил вовремя. Перед повтором проверьте доставку."
    except Exception:
        status, error = "unknown", "Не удалось подтвердить отправку. Перед повтором проверьте доставку."

    async with SessionMaker.begin() as session:
        result = await session.execute(update(ManualReceipt).where(
            ManualReceipt.attempt_id == attempt_id, ManualReceipt.status == "sending",
            ManualReceipt.send_started_at == started_at,
        ).values(status=status, error=error, send_after=next_attempt,
                 sent_at=_now() if status == "sent" else None))
        if result.rowcount and status == "sent" and combined:
            await enqueue_delivery(session, order_id, channels=("telegram",))
    if status in {"failed", "unknown"}:
        logger.error("Письмо с чеком по заказу №%s: %s", order_id, error)


async def _recover_sends() -> None:
    async with SessionMaker.begin() as session:
        await session.execute(update(ManualReceipt).where(
            ManualReceipt.status == "sending",
            ManualReceipt.send_started_at < _now() - timedelta(minutes=5),
        ).values(status="unknown", error="После перезапуска результат отправки неизвестен. Проверьте доставку."))


async def receipt_worker(bot: Bot, *, notifications: bool) -> None:
    while True:
        try:
            if notifications:
                await _notify_one(bot)
            else:
                await _recover_sends()
                await _send_one(bot)
        except Exception:
            logger.exception("Ошибка очереди чеков")
        await asyncio.sleep(3)
