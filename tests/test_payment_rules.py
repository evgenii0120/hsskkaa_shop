from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.payment_rules import (
    can_retry_idempotent_create,
    delivery_retry_delay,
    validate_payment_attempt,
)


def test_idempotency_retry_window_is_conservative():
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    assert can_retry_idempotent_create(now - timedelta(hours=22), now, 23)
    assert not can_retry_idempotent_create(now - timedelta(hours=23), now, 23)
    assert not can_retry_idempotent_create(now - timedelta(hours=25), now, 23)


def test_idempotency_window_normalizes_naive_utc_datetime():
    now = datetime(2026, 1, 1, 12)
    assert can_retry_idempotent_create(now - timedelta(hours=1), now, 23)


def test_payment_attempt_identity_accepts_matching_attempt():
    validate_payment_attempt(
        attempt_id=7, order_id=42, expected_amount=Decimal("500"), expected_currency="RUB",
        metadata={"order_id": "42", "payment_attempt_id": "7"},
        actual_amount=Decimal("500.00"), actual_currency="RUB",
    )


@pytest.mark.parametrize("metadata", [
    {"order_id": "43", "payment_attempt_id": "7"},
    {"order_id": "42", "payment_attempt_id": "8"},
])
def test_payment_attempt_rejects_wrong_metadata(metadata):
    with pytest.raises(ValueError):
        validate_payment_attempt(
            attempt_id=7, order_id=42, expected_amount=Decimal("500"), expected_currency="RUB",
            metadata=metadata, actual_amount=Decimal("500"), actual_currency="RUB",
        )


def test_payment_attempt_rejects_amount_or_currency_mismatch():
    with pytest.raises(ValueError):
        validate_payment_attempt(
            attempt_id=7, order_id=42, expected_amount=Decimal("500"), expected_currency="RUB",
            metadata={"order_id": "42", "payment_attempt_id": "7"},
            actual_amount=Decimal("499.99"), actual_currency="RUB",
        )
    with pytest.raises(ValueError):
        validate_payment_attempt(
            attempt_id=7, order_id=42, expected_amount=Decimal("500"), expected_currency="RUB",
            metadata={"order_id": "42", "payment_attempt_id": "7"},
            actual_amount=Decimal("500"), actual_currency="USD",
        )


def test_delivery_backoff_increases_and_caps():
    assert delivery_retry_delay(1) == timedelta(minutes=1)
    assert delivery_retry_delay(2) == timedelta(minutes=5)
    assert delivery_retry_delay(7) == timedelta(hours=6)
    assert delivery_retry_delay(100) == timedelta(hours=6)


def test_multiple_payment_attempts_for_same_order_are_distinguishable():
    common = dict(
        order_id=42, expected_amount=Decimal("500"), expected_currency="RUB",
        actual_amount=Decimal("500.00"), actual_currency="RUB",
    )
    validate_payment_attempt(
        attempt_id=7, metadata={"order_id": "42", "payment_attempt_id": "7"}, **common
    )
    validate_payment_attempt(
        attempt_id=8, metadata={"order_id": "42", "payment_attempt_id": "8"}, **common
    )
