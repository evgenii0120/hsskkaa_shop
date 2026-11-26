import asyncio
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from yookassa import Configuration, Payment

from config import (
    YOOKASSA_RETURN_URL, YOOKASSA_SECRET_KEY, YOOKASSA_SHOP_ID,
)

if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)


def is_configured() -> bool:
    return bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)


def money(value: int | float | Decimal) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def build_payload(*, amount: int | Decimal, order_id: int, attempt_id: int,
                  description: str, return_url: str) -> dict[str, Any]:
    amount_obj = {"value": money(amount), "currency": "RUB"}
    return {
        "amount": amount_obj,
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url or YOOKASSA_RETURN_URL or "https://t.me/"},
        "description": f"Заказ №{order_id}: {description}"[:128],
        "metadata": {"order_id": str(order_id), "payment_attempt_id": str(attempt_id)},
    }


async def create_payment(payload: dict[str, Any], idempotency_key: str):
    if not is_configured():
        raise RuntimeError("Не заданы YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY")
    return await asyncio.to_thread(Payment.create, payload, idempotency_key)


async def get_payment(payment_id: str):
    if not is_configured():
        raise RuntimeError("ЮKassa не настроена")
    return await asyncio.to_thread(Payment.find_one, payment_id)


def payment_amount(payment: Any) -> Decimal:
    return Decimal(str(payment.amount.value)).quantize(Decimal("0.01"))


def payment_metadata(payment: Any) -> dict[str, Any]:
    value = getattr(payment, "metadata", None)
    return dict(value or {})


def confirmation_url(payment: Any) -> str | None:
    confirmation = getattr(payment, "confirmation", None)
    return getattr(confirmation, "confirmation_url", None) if confirmation else None


def payment_account_id(payment: Any) -> str | None:
    recipient = getattr(payment, "recipient", None)
    value = getattr(recipient, "account_id", None) if recipient else None
    return str(value) if value is not None else None


async def list_payments(created_from: datetime, created_to: datetime, limit: int = 100):
    if not is_configured():
        raise RuntimeError("ЮKassa не настроена")
    base = {
        "created_at.gte": created_from.isoformat(),
        "created_at.lte": created_to.isoformat(),
        "limit": min(max(int(limit), 1), 100),
    }
    collected = []
    cursor = None
    for _ in range(100):
        params = dict(base)
        if cursor:
            params["cursor"] = cursor
        response = await asyncio.to_thread(Payment.list, params)
        collected.extend(list(getattr(response, "items", []) or []))
        cursor = getattr(response, "next_cursor", None)
        if not cursor:
            return collected
    raise RuntimeError("YooKassa payment list exceeded 100 pages; reconciliation is incomplete")
