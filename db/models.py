from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey,
    Index, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    fio: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    consent_given_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @property
    def is_registered(self) -> bool:
        return bool(self.consent_given_at and self.fio and self.email)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    subtitle: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    includes: Mapped[list] = mapped_column(JSON, default=list)
    price: Mapped[int] = mapped_column(Integer)
    emoji: Mapped[str] = mapped_column(String(8), default="📘")
    file_format: Mapped[str] = mapped_column(String(20), default="PDF")
    file_id: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    amount: Mapped[int] = mapped_column(Integer)
    # Immutable checkout-time customer snapshot; legacy rows are backfilled by migration.
    customer_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    customer_fio: Mapped[str | None] = mapped_column(String(255))
    customer_email: Mapped[str | None] = mapped_column(String(255))
    # Legacy compatibility only. PaymentAttempt and DeliveryTask are authoritative.
    status: Mapped[str] = mapped_column(String(20), default="pending")
    payment_id: Mapped[str | None] = mapped_column(String(255))
    secret_code: Mapped[str | None] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('creating','creation_unknown','creation_failed','pending','succeeded','canceled','reconciliation_required')",
            name="ck_payment_attempts_status",
        ),
        CheckConstraint("amount >= 0", name="ck_payment_attempts_amount_nonnegative"),
        UniqueConstraint("idempotency_key", name="uq_payment_attempts_idempotency_key"),
        UniqueConstraint("payment_id", name="uq_payment_attempts_payment_id"),
        Index("ix_payment_attempts_order_id", "order_id"),
        Index("ix_payment_attempts_status", "status"),
        Index("ix_payment_attempts_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(36))
    payment_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="creating", nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="RUB", nullable=False)
    confirmation_url: Mapped[str | None] = mapped_column(Text)
    provider_metadata: Mapped[dict | None] = mapped_column(JSON)
    request_payload: Mapped[dict | None] = mapped_column(JSON)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ManualReceipt(Base):
    __tablename__ = "manual_receipts"
    __table_args__ = (
        Index("ix_manual_receipts_mail", "status", "send_after"),
        Index("ix_manual_receipts_notice", "notify_after"),
    )

    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("payment_attempts.id", ondelete="RESTRICT"), primary_key=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    pdf: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    pdf_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    uploaded_by: Mapped[int | None] = mapped_column(BigInteger)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    send_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    send_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    send_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    notify_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)


class DeliveryTask(Base):
    __tablename__ = "delivery_tasks"
    __table_args__ = (
        UniqueConstraint("order_id", "channel", name="uq_delivery_order_channel"),
        CheckConstraint("channel IN ('telegram','email')", name="ck_delivery_channel"),
        CheckConstraint("status IN ('pending','processing','retry_wait','delivered','blocked','skipped')", name="ck_delivery_status"),
        CheckConstraint("attempts >= 0", name="ck_delivery_attempts_nonnegative"),
        Index("ix_delivery_tasks_ready", "status", "next_retry_at"),
        Index("ix_delivery_tasks_expired", "status", "locked_until"),
        Index("ix_delivery_tasks_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=_now)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_delivery_attempt_number"),
        CheckConstraint("status IN ('started','accepted','failed','unknown')", name="ck_delivery_attempt_status"),
        CheckConstraint("attempt_number > 0", name="ck_delivery_attempt_number_positive"),
        Index("ix_delivery_attempts_history", "task_id", "started_at"),
        Index("ix_delivery_attempts_status", "status", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("delivery_tasks.id", ondelete="RESTRICT"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="started", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_type: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    external_message_id: Mapped[str | None] = mapped_column(String(255))
    result_metadata: Mapped[dict | None] = mapped_column(JSON)


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="open")
    admin_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TicketMessage(Base):
    __tablename__ = "ticket_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    sender_id: Mapped[int] = mapped_column(BigInteger)
    message_type: Mapped[str] = mapped_column(String(20), default="text")
    content: Mapped[str] = mapped_column(Text, default="")
    file_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Settings(Base):
    __tablename__ = "settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, default="")
