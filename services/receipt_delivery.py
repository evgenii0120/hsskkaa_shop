"""Track fiscal registration separately from payment and product delivery."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from config import RECEIPT_EMAIL_COPY, YOOKASSA_SEND_RECEIPT
from db.base import SessionMaker
from db.models import Order, PaymentAttempt, PaymentReceipt
from services import yookassa_service as yk
from services.email_service import SMTPDeliveryError, send_receipt_email

logger = logging.getLogger(__name__)
CHECK_INTERVAL = timedelta(minutes=2)
CHECK_WINDOW = timedelta(days=3)
SEND_TIMEOUT = timedelta(minutes=10)
SEND_RECOVERY = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _receipt_email(attempt: PaymentAttempt, order: Order) -> str | None:
    receipt = (attempt.request_payload or {}).get("receipt") or {}
    email = (receipt.get("customer") or {}).get("email")
    if not email and attempt.idempotency_key is not None:
        email = order.customer_email
    return email.strip() if email and email.strip() else None


async def track_paid_attempt(session, attempt_id: int, *, auto_email: bool) -> None:
    now = _now()
    send = auto_email and YOOKASSA_SEND_RECEIPT and RECEIPT_EMAIL_COPY
    await session.execute(insert(PaymentReceipt).values(
        attempt_id=attempt_id, status="waiting", next_check_at=now,
        email_status="queued" if send else None,
        email_requested_at=now if send else None,
    ).on_conflict_do_nothing(index_elements=[PaymentReceipt.attempt_id]))


async def queue_receipt_email(attempt_id: int) -> str:
    if not yk.is_configured():
        return "ЮKassa не настроена."
    async with SessionMaker.begin() as session:
        row = (await session.execute(
            select(PaymentAttempt, Order).join(Order, Order.id == PaymentAttempt.order_id)
            .where(PaymentAttempt.id == attempt_id)
            .with_for_update(of=PaymentAttempt)
        )).one_or_none()
        if row is None or not row[0].payment_id:
            return "У попытки нет ID платежа ЮKassa."
        attempt, order = row
        if not _receipt_email(attempt, order):
            return "В заказе нет подтверждённого адреса из данных покупки."
        await track_paid_attempt(session, attempt.id, auto_email=False)
        tracking = await session.get(PaymentReceipt, attempt.id, with_for_update=True)
        if tracking.email_status == "sending":
            return "Письмо уже отправляется."
        if tracking.email_status == "queued":
            tracking.next_check_at = _now()
            return "Письмо уже в очереди. Внеплановая проверка чека поставлена в очередь."
        now = _now()
        tracking.email_status = "queued"
        tracking.email_requested_at = now
        tracking.email_error = None
        tracking.next_check_at = now
    return "Запрос поставлен в очередь. Реквизиты отправятся после проверки чека в ЮKassa."


def _fiscal_documents(receipts, payment_id: str) -> tuple[list[dict], set[str]]:
    documents = {}
    statuses = set()
    for receipt in _payment_receipts(receipts, payment_id):
        statuses.add(receipt.status)
        if receipt.status != "succeeded":
            continue
        fiscal = {
            "storage": getattr(receipt, "fiscal_storage_number", None),
            "document": getattr(receipt, "fiscal_document_number", None),
            "attribute": getattr(receipt, "fiscal_attribute", None),
            "registered_at": getattr(receipt, "registered_at", None),
        }
        if not all(fiscal.values()):
            continue
        receipt_id = str(receipt.id)
        documents[receipt_id] = {"id": receipt_id, **{key: str(value) for key, value in fiscal.items()}}
    return list(documents.values()), statuses



def _payment_receipts(receipts, payment_id: str) -> list:
    return [
        receipt for receipt in receipts
        if receipt.type == "payment" and getattr(receipt, "payment_id", None) in (None, payment_id)
    ]


async def _lookup_receipt(tracking: PaymentReceipt, attempt: PaymentAttempt, order: Order) -> list[dict]:
    payment_id = attempt.payment_id
    if not payment_id:
        tracking.status = "no_payment_id"
        return []

    payment = await yk.get_payment(payment_id)
    tracking.registration_status = getattr(payment, "receipt_registration", None)
    remote_order = yk.payment_metadata(payment).get("order_id")
    if str(payment.id) != payment_id or (remote_order is not None and str(remote_order) != str(order.id)):
        tracking.status = "mismatch"
        return []
    if payment.test:
        tracking.status = "test"
        return []

    receipts = await yk.list_payment_receipts(payment_id)
    candidates = _payment_receipts(receipts, payment_id)
    documents, statuses = _fiscal_documents(candidates, payment_id)
    complete_ids = {document["id"] for document in documents}
    for receipt in candidates:
        if receipt.status == "succeeded" and receipt.id and str(receipt.id) not in complete_ids:
            try:
                detail = await asyncio.wait_for(yk.get_receipt(str(receipt.id)), timeout=10)
            except Exception as exc:
                logger.warning(
                    "Receipt details failed attempt=%s receipt_id=%s error_type=%s",
                    attempt.id, receipt.id, type(exc).__name__,
                )
                continue
            if str(detail.id) != str(receipt.id):
                continue
            recovered, detail_statuses = _fiscal_documents([detail], payment_id)
            documents.extend(recovered)
            complete_ids.update(document["id"] for document in recovered)
            statuses.update(detail_statuses)

    tracking.fiscal_documents = documents
    if documents:
        tracking.status = "registered"
    elif "succeeded" in statuses or tracking.registration_status == "succeeded":
        tracking.status = "details_unavailable"
    elif "pending" in statuses or tracking.registration_status == "pending":
        tracking.status = "pending"
    elif "canceled" in statuses or tracking.registration_status == "canceled":
        tracking.status = "canceled"
    elif payment.status == "canceled":
        tracking.status = "not_applicable"
    elif not receipts:
        tracking.status = "api_empty"
    elif not candidates:
        tracking.status = "no_payment_receipt"
    else:
        tracking.status = "unconfirmed"

    logger.info(
        "Receipt lookup attempt=%s payment_id=%s payment_status=%s receipt_registration=%s "
        "api_receipts=%s matching_payment_receipts=%s fiscal_documents=%s result=%s",
        attempt.id, payment_id, payment.status, tracking.registration_status,
        len(receipts), len(candidates), len(documents), tracking.status,
    )
    return documents

def _email_text(order_id: int, documents: list[dict]) -> str:
    lines = [f"Фискальные реквизиты чека прихода по заказу №{order_id}, полученные из ЮKassa.", ""]
    for document in documents:
        lines.extend((
            f"Дата и время регистрации: {document['registered_at']}",
            f"ФН: {document['storage']}",
            f"ФД: {document['document']}",
            f"ФП: {document['attribute']}",
            "",
        ))
    lines.append("Проверить и найти чек можно через сервис «Проверка чека ФНС России»:")
    lines.append("https://kkt-online.nalog.ru/")
    return "\n".join(lines)


async def _prepare_tracking() -> None:
    now = _now()
    async with SessionMaker.begin() as session:
        # A crashed process may have reached SMTP DATA. Do not retry it automatically.
        await session.execute(update(PaymentReceipt).where(
            PaymentReceipt.email_status == "sending",
            PaymentReceipt.email_started_at < now - SEND_RECOVERY,
        ).values(email_status="unknown", email_error="Результат отправки после перезапуска неизвестен"))
        attempts = (await session.scalars(
            select(PaymentAttempt.id).outerjoin(PaymentReceipt).where(
                PaymentAttempt.payment_id.is_not(None),
                PaymentAttempt.status.in_(("succeeded", "reconciliation_required")),
                PaymentReceipt.attempt_id.is_(None),
            ).order_by(PaymentAttempt.id.desc()).limit(50)
        )).all()
        if attempts:
            await session.execute(insert(PaymentReceipt).values([
                {"attempt_id": attempt_id, "status": "waiting", "next_check_at": now}
                for attempt_id in attempts
            ]).on_conflict_do_nothing(index_elements=[PaymentReceipt.attempt_id]))


def _record_failure(tracking: PaymentReceipt, attempt_id: int, reason: str) -> None:
    tracking.email_status = "blocked"
    tracking.email_error = reason
    logger.error("Receipt email blocked for attempt %s: %s", attempt_id, reason)


async def _check_one() -> bool:
    outgoing = None
    async with SessionMaker.begin() as session:
        row = (await session.execute(
            select(PaymentReceipt, PaymentAttempt, Order)
            .join(PaymentAttempt, PaymentAttempt.id == PaymentReceipt.attempt_id)
            .join(Order, Order.id == PaymentAttempt.order_id)
            .where(PaymentReceipt.next_check_at <= _now())
            .order_by(
                PaymentReceipt.email_requested_at.is_(None),
                PaymentReceipt.next_check_at,
                PaymentReceipt.attempt_id,
            )
            .limit(1)
            .with_for_update(of=PaymentReceipt, skip_locked=True)
        )).one_or_none()
        if row is None:
            return False
        tracking, attempt, order = row
        payment_id = attempt.payment_id
        now = _now()
        tracking.checked_at = now
        tracking.next_check_at = None
        requested = tracking.email_status == "queued"
        since = tracking.email_requested_at if requested else attempt.paid_at
        deadline = (since or attempt.created_at) + CHECK_WINDOW
        try:
            documents = await asyncio.wait_for(_lookup_receipt(tracking, attempt, order), timeout=50)
        except Exception as exc:
            logger.warning(
                "Receipt lookup failed attempt=%s payment_id=%s error_type=%s",
                attempt.id, payment_id, type(exc).__name__,
            )
            tracking.status = "api_error"
            documents = []
        retryable = tracking.status in {
            "pending", "api_empty", "no_payment_receipt", "unconfirmed", "details_unavailable", "api_error",
        }
        if payment_id and retryable and now < deadline:
            tracking.next_check_at = now + CHECK_INTERVAL
        elif requested:
            if not documents:
                _record_failure(tracking, attempt.id, "Проверка завершена. Бот не получил реквизиты для отправки.")
            else:
                email = _receipt_email(attempt, order)
                if email is None:
                    _record_failure(tracking, attempt.id, "Отсутствует адрес покупателя из данных заказа.")
                else:
                    tracking.email_status = "sending"
                    tracking.email_started_at = now
                    outgoing = (attempt.id, now, order.id, email, documents)
    if outgoing:
        await _send(*outgoing)
    return True


async def _send(attempt_id: int, started: datetime, order_id: int, email: str, documents: list[dict]) -> None:
    status = "accepted"
    error = None
    message_id = f"<receipt-{attempt_id}-{uuid4().hex}@hsskkaa.ru>"
    try:
        await asyncio.wait_for(send_receipt_email(
            email=email, text=_email_text(order_id, documents), message_id=message_id, order_id=order_id,
        ), timeout=SEND_TIMEOUT.total_seconds())
    except SMTPDeliveryError as exc:
        status = "unknown" if exc.ambiguous else "failed"
        error = f"Ошибка SMTP на этапе {exc.stage}."
    except TimeoutError:
        status = "unknown"
        error = "Истекло время ожидания SMTP."
    except Exception as exc:
        status = "unknown"
        error = f"Результат отправки неизвестен ({type(exc).__name__})."
    values = {"email_status": status, "email_error": error}
    if error:
        logger.error("Receipt email for attempt %s: %s", attempt_id, error)
    else:
        values["email_accepted_at"] = _now()
    async with SessionMaker.begin() as session:
        await session.execute(update(PaymentReceipt).where(
            PaymentReceipt.attempt_id == attempt_id,
            PaymentReceipt.email_status == "sending",
            PaymentReceipt.email_started_at == started,
        ).values(**values))


async def receipt_worker() -> None:
    while True:
        try:
            await _prepare_tracking()
            for _ in range(10):
                if not await _check_one():
                    break
        except Exception:
            logger.exception("Receipt worker failed")
        await asyncio.sleep(5)
