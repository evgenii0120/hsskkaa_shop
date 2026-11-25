from datetime import datetime, timedelta, timezone
from decimal import Decimal


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def can_retry_idempotent_create(created_at: datetime, now: datetime, safe_hours: int = 23) -> bool:
    return as_utc(now) - as_utc(created_at) < timedelta(hours=safe_hours)


def validate_payment_attempt(*, attempt_id: int, order_id: int, expected_amount: Decimal,
                              expected_currency: str, metadata: dict,
                              actual_amount: Decimal, actual_currency: str) -> None:
    if str(metadata.get("order_id")) != str(order_id):
        raise ValueError("metadata.order_id mismatch")
    metadata_attempt_id = metadata.get("payment_attempt_id")
    if metadata_attempt_id is not None and str(metadata_attempt_id) != str(attempt_id):
        raise ValueError("metadata.payment_attempt_id mismatch")
    if Decimal(str(actual_amount)).quantize(Decimal("0.01")) != Decimal(str(expected_amount)).quantize(Decimal("0.01")):
        raise ValueError("payment amount mismatch")
    if str(actual_currency) != str(expected_currency):
        raise ValueError("payment currency mismatch")


def delivery_retry_delay(attempt_number: int, max_hours: int = 6) -> timedelta:
    intervals = (1, 5, 15, 30, 60, 180, 360)
    minutes = intervals[min(max(attempt_number - 1, 0), len(intervals) - 1)]
    return min(timedelta(minutes=minutes), timedelta(hours=max_hours))
