from pathlib import Path


def test_legacy_payment_attempt_lookup_is_by_payment_id_only():
    source = (Path(__file__).parents[1] / "db" / "migrate.py").read_text(encoding="utf-8")
    assert "WHERE payment_id = :payment_id" in source
    assert "WHERE payment_id = :payment_id OR order_id = :order_id" not in source
    assert "already belongs to order" in source


def test_migration_uses_transactional_advisory_lock_and_one_time_marker():
    source = (Path(__file__).parents[1] / "db" / "migrate.py").read_text(encoding="utf-8")
    assert "pg_advisory_xact_lock" in source
    assert "shop_schema_migrations" in source
    assert "ON CONFLICT (order_id, channel) DO NOTHING" in source


def test_delivery_attempt_history_guard_is_terminal_and_append_only():
    source = (Path(__file__).parents[1] / "db" / "migrate.py").read_text(encoding="utf-8")
    assert "DeliveryAttempt history cannot be deleted" in source
    assert "Completed DeliveryAttempt is immutable" in source
    assert "BEFORE INSERT OR UPDATE OR DELETE ON delivery_attempts" in source
    assert "NEW.status NOT IN ('accepted', 'failed', 'unknown')" in source


def test_delivery_queue_uses_skip_locked_and_lease_fencing():
    source = (Path(__file__).parents[1] / "services" / "delivery.py").read_text(encoding="utf-8")
    assert ".with_for_update(skip_locked=True)" in source
    assert "DeliveryTask.lease_token == claim.lease_token" in source
    assert "locked_until" in source
    assert "WorkerLeaseExpired" in source
