"""Explicit, transactional PostgreSQL schema migration. Run: python -m db.migrate"""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import inspect, text

from db.base import Base, engine

LOCK_KEY_1 = 741923
LOCK_KEY_2 = 1
LEGACY_REVISION = "20261109_payment_delivery_v1"


def _legacy_columns(conn) -> None:
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    if "users" in tables:
        columns = {c["name"] for c in inspector.get_columns("users")}
        if "consent_given_at" not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN consent_given_at TIMESTAMP WITH TIME ZONE"))
        if "is_banned" not in columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_banned BOOLEAN NOT NULL DEFAULT FALSE"))
    if "orders" in tables:
        order_columns = {c["name"] for c in inspector.get_columns("orders")}
        if "customer_telegram_id" not in order_columns:
            conn.execute(text("ALTER TABLE orders ADD COLUMN customer_telegram_id BIGINT"))
        if "customer_fio" not in order_columns:
            conn.execute(text("ALTER TABLE orders ADD COLUMN customer_fio VARCHAR(255)"))
        if "customer_email" not in order_columns:
            conn.execute(text("ALTER TABLE orders ADD COLUMN customer_email VARCHAR(255)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_orders_payment_id ON orders (payment_id)"))
        conn.execute(text("""
            UPDATE orders AS o SET
                customer_telegram_id = COALESCE(o.customer_telegram_id, u.telegram_id),
                customer_fio = COALESCE(o.customer_fio, u.fio),
                customer_email = COALESCE(o.customer_email, u.email)
            FROM users AS u WHERE u.id = o.user_id
        """))

    # Forward-compatible columns for an earlier manually applied first-stage schema.
    if "payment_attempts" in tables:
        columns = {c["name"] for c in inspector.get_columns("payment_attempts")}
        if "request_payload" not in columns:
            conn.execute(text("ALTER TABLE payment_attempts ADD COLUMN request_payload JSON"))
    if "delivery_tasks" in tables:
        columns = {c["name"] for c in inspector.get_columns("delivery_tasks")}
        if "lease_token" not in columns:
            conn.execute(text("ALTER TABLE delivery_tasks ADD COLUMN lease_token VARCHAR(36)"))

    if "payment_attempts" in tables:
        duplicates = conn.execute(text("""
            SELECT order_id, COUNT(*) AS n
            FROM payment_attempts
            WHERE status IN ('creating', 'creation_unknown', 'pending')
            GROUP BY order_id HAVING COUNT(*) > 1
            LIMIT 10
        """)).all()
        if duplicates:
            raise RuntimeError(
                "Several active PaymentAttempt exist for the same order; "
                f"manual reconciliation is required: {duplicates!r}"
            )
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_attempts_one_active_per_order
            ON payment_attempts (order_id)
            WHERE status IN ('creating', 'creation_unknown', 'pending')
        """))
        conn.execute(text("ALTER TABLE payment_attempts DROP CONSTRAINT IF EXISTS ck_payment_attempts_status"))
        conn.execute(text("ALTER TABLE payment_attempts ADD CONSTRAINT ck_payment_attempts_status CHECK (status IN ('creating','creation_unknown','creation_failed','pending','succeeded','canceled','reconciliation_required'))"))
        conn.execute(text("ALTER TABLE payment_attempts DROP CONSTRAINT IF EXISTS ck_payment_attempts_amount_positive"))
        conn.execute(text("ALTER TABLE payment_attempts DROP CONSTRAINT IF EXISTS ck_payment_attempts_amount_nonnegative"))
        conn.execute(text("ALTER TABLE payment_attempts ADD CONSTRAINT ck_payment_attempts_amount_nonnegative CHECK (amount >= 0)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_payment_attempts_order_id ON payment_attempts (order_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_payment_attempts_status ON payment_attempts (status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_payment_attempts_created_at ON payment_attempts (created_at)"))

    if "delivery_tasks" in tables:
        conn.execute(text("ALTER TABLE delivery_tasks DROP CONSTRAINT IF EXISTS ck_delivery_channel"))
        conn.execute(text("ALTER TABLE delivery_tasks ADD CONSTRAINT ck_delivery_channel CHECK (channel IN ('telegram','email'))"))
        conn.execute(text("ALTER TABLE delivery_tasks DROP CONSTRAINT IF EXISTS ck_delivery_status"))
        conn.execute(text("ALTER TABLE delivery_tasks ADD CONSTRAINT ck_delivery_status CHECK (status IN ('pending','processing','retry_wait','delivered','blocked','skipped'))"))
        conn.execute(text("ALTER TABLE delivery_tasks DROP CONSTRAINT IF EXISTS ck_delivery_attempts_nonnegative"))
        conn.execute(text("ALTER TABLE delivery_tasks ADD CONSTRAINT ck_delivery_attempts_nonnegative CHECK (attempts >= 0)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_delivery_tasks_ready ON delivery_tasks (status, next_retry_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_delivery_tasks_expired ON delivery_tasks (status, locked_until)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_delivery_tasks_order_id ON delivery_tasks (order_id)"))

    if "delivery_attempts" in tables:
        conn.execute(text("ALTER TABLE delivery_attempts DROP CONSTRAINT IF EXISTS ck_delivery_attempt_status"))
        conn.execute(text("ALTER TABLE delivery_attempts ADD CONSTRAINT ck_delivery_attempt_status CHECK (status IN ('started','accepted','failed','unknown'))"))
        conn.execute(text("ALTER TABLE delivery_attempts DROP CONSTRAINT IF EXISTS ck_delivery_attempt_number_positive"))
        conn.execute(text("ALTER TABLE delivery_attempts ADD CONSTRAINT ck_delivery_attempt_number_positive CHECK (attempt_number > 0)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_delivery_attempts_history ON delivery_attempts (task_id, started_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_delivery_attempts_status ON delivery_attempts (status, started_at)"))


def _legacy_data(conn) -> None:
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS shop_schema_migrations (
            revision VARCHAR(100) PRIMARY KEY,
            applied_at TIMESTAMP WITH TIME ZONE NOT NULL
        )
    """))
    already = conn.execute(
        text("SELECT 1 FROM shop_schema_migrations WHERE revision = :revision"),
        {"revision": LEGACY_REVISION},
    ).scalar_one_or_none()
    if already:
        return
    tables = set(inspect(conn).get_table_names())
    if "orders" not in tables:
        return
    rows = conn.execute(text("SELECT id, status, payment_id, amount FROM orders ORDER BY id")).mappings().all()
    now = datetime.now(timezone.utc)
    for order in rows:
        order_id = int(order["id"])
        payment_id = order["payment_id"]
        if payment_id:
            # Only payment_id identifies the legacy attempt. An order can have many attempts.
            existing = conn.execute(
                text("SELECT id, order_id FROM payment_attempts WHERE payment_id = :payment_id"),
                {"payment_id": payment_id},
            ).mappings().one_or_none()
            if existing and int(existing["order_id"]) != order_id:
                raise RuntimeError(
                    f"payment_id {payment_id!r} already belongs to order {existing['order_id']}, not {order_id}"
                )
            if existing is None:
                conn.execute(text("""
                    INSERT INTO payment_attempts
                    (order_id, payment_id, status, amount, currency, created_at, updated_at)
                    VALUES (:order_id, :payment_id, 'reconciliation_required', :amount, 'RUB', :now, :now)
                """), {
                    "order_id": order_id,
                    "payment_id": payment_id,
                    "amount": Decimal(str(order["amount"])),
                    "now": now,
                })
        # Never infer delivery success from legacy order.status. Any order carrying
        # a legacy payment_id, plus every legacy succeeded order, is held for review.
        if payment_id or order["status"] == "succeeded":
            for channel in ("telegram", "email"):
                conn.execute(text("""
                    INSERT INTO delivery_tasks
                    (order_id, channel, status, attempts, last_error, created_at, updated_at)
                    VALUES (:order_id, :channel, 'blocked', 0,
                            'Legacy order requires manual reconciliation; automatic resend disabled', :now, :now)
                    ON CONFLICT (order_id, channel) DO NOTHING
                """), {"order_id": order_id, "channel": channel, "now": now})
                # A pre-existing non-delivered task must never be auto-retried as part
                # of the legacy migration. Already delivered/skipped tasks are preserved.
                conn.execute(text("""
                    UPDATE delivery_tasks
                    SET status = 'blocked', next_retry_at = NULL,
                        locked_until = NULL, locked_by = NULL, lease_token = NULL,
                        last_error = 'Legacy order requires manual reconciliation; automatic resend disabled',
                        updated_at = :now
                    WHERE order_id = :order_id AND channel = :channel
                      AND status IN ('pending', 'processing', 'retry_wait')
                """), {"order_id": order_id, "channel": channel, "now": now})
                conn.execute(text("""
                    UPDATE delivery_attempts
                    SET status = 'unknown', finished_at = :now,
                        error_type = 'LegacyMigration',
                        error_message = 'Legacy delivery result requires manual reconciliation'
                    WHERE task_id IN (SELECT id FROM delivery_tasks WHERE order_id = :order_id AND channel = :channel)
                      AND status = 'started'
                """), {"order_id": order_id, "channel": channel, "now": now})

    conn.execute(
        text("INSERT INTO shop_schema_migrations (revision, applied_at) VALUES (:revision, :now)"),
        {"revision": LEGACY_REVISION, "now": now},
    )


def _delivery_history_guard(conn) -> None:
    conn.execute(text("""
        CREATE OR REPLACE FUNCTION guard_delivery_attempt_history()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'started' THEN
                    RAISE EXCEPTION 'New DeliveryAttempt must have started status';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'DeliveryAttempt history cannot be deleted';
            END IF;
            IF OLD.status <> 'started' THEN
                RAISE EXCEPTION 'Completed DeliveryAttempt is immutable';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.task_id IS DISTINCT FROM OLD.task_id
               OR NEW.attempt_number IS DISTINCT FROM OLD.attempt_number
               OR NEW.started_at IS DISTINCT FROM OLD.started_at THEN
                RAISE EXCEPTION 'DeliveryAttempt identity fields are immutable';
            END IF;
            IF NEW.status NOT IN ('accepted', 'failed', 'unknown') OR NEW.finished_at IS NULL THEN
                RAISE EXCEPTION 'Invalid DeliveryAttempt completion';
            END IF;
            RETURN NEW;
        END;
        $$
    """))
    conn.execute(text("DROP TRIGGER IF EXISTS trg_delivery_attempts_immutable ON delivery_attempts"))
    conn.execute(text("""
        CREATE TRIGGER trg_delivery_attempts_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON delivery_attempts
        FOR EACH ROW EXECUTE FUNCTION guard_delivery_attempt_history()
    """))


async def migrate() -> None:
    from db import models  # noqa: F401
    if engine.dialect.name != "postgresql":
        raise RuntimeError("Для миграции нужна база PostgreSQL")
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:a, :b)"), {"a": LOCK_KEY_1, "b": LOCK_KEY_2})
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_legacy_columns)
        await conn.run_sync(_legacy_data)
        await conn.run_sync(_delivery_history_guard)
    print("Миграция PostgreSQL завершена.")


if __name__ == "__main__":
    import asyncio

    async def _main() -> None:
        try:
            await migrate()
        finally:
            await engine.dispose()

    asyncio.run(_main())
