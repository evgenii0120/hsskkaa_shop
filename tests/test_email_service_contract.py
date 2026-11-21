from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_smtp_client_has_separate_connect_auth_and_send_stages():
    source = (ROOT / "services" / "email_service.py").read_text(encoding="utf-8")
    assert "await smtp.connect()" in source
    assert "await smtp.login(SMTP_USER, SMTP_PASSWORD)" in source
    assert "await smtp.send_message(message)" in source
    assert '"connect"' in source
    assert '"authentication"' in source
    assert '"send"' in source


def test_smtp_config_rejects_conflicting_tls_modes_and_port_mismatch():
    source = (ROOT / "services" / "email_service.py").read_text(encoding="utf-8")
    assert "SMTP_USE_TLS and SMTP_START_TLS" in source
    assert "SMTP_PORT == 465" in source
    assert "SMTP_PORT == 587" in source


def test_email_error_stages_do_not_change_telegram_delivery_task():
    source = (ROOT / "services" / "delivery.py").read_text(encoding="utf-8")
    assert "except SMTPDeliveryError as exc:" in source
    assert 'await _finish(claim, "failed_permanent", error=exc)' in source
    assert 'await _finish(claim, "unknown", error=exc)' in source
    assert 'await _finish(claim, "retryable", error=exc)' in source
    assert 'for channel in ("telegram", "email")' in source


def test_admin_retry_is_per_delivery_task_not_whole_order():
    source = (ROOT / "services" / "delivery.py").read_text(encoding="utf-8")
    body = source.split("async def force_retry_task", 1)[1]
    assert "select(DeliveryTask).where(DeliveryTask.id == task_id)" in body
    assert 'task.status in {"delivered", "processing"}' in body
    assert 'task.status = "pending"' in body


def test_smtp_diagnostic_never_authenticates_or_sends():
    source = (ROOT / "tools" / "smtp_diagnostic.py").read_text(encoding="utf-8")
    assert "client.login" not in source
    assert "client.sendmail" not in source
    assert "No login or email was attempted" in source
