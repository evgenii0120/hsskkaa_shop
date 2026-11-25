import asyncio
import logging
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address, ip_network

from aiohttp import web
from aiogram import Bot
from sqlalchemy import select, update

from config import (ADMIN_IDS, DEBUG_ADMIN_IDS, WEBHOOK_CHECK_IP, WEBHOOK_HOST,
                    WEBHOOK_PATH, WEBHOOK_PORT, WEBHOOK_TRUST_PROXY, YOOKASSA_SHOP_ID)
from db.base import SessionMaker
from db.models import Order, PaymentAttempt
from services.delivery import enqueue_delivery
from services import yookassa_service as yk
from services.payment_rules import validate_payment_attempt
from services.receipt_delivery import track_paid_attempt

YOOKASSA_NETWORKS = [ip_network(n) for n in (
    "185.71.76.0/27", "185.71.77.0/27", "77.75.153.0/25",
    "77.75.156.11/32", "77.75.156.35/32", "77.75.154.128/25", "2a02:5180::/32",
)]


def _client_ip(request: web.Request) -> str | None:
    if WEBHOOK_TRUST_PROXY:
        real = request.headers.get("X-Real-IP")
        if real:
            return real.strip()
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.remote


def _ip_allowed(value: str | None) -> bool:
    try:
        addr = ip_address(value or "")
    except ValueError:
        return False
    return any(addr in network for network in YOOKASSA_NETWORKS)


async def _notify_admins(bot: Bot, message: str) -> None:
    for admin_id in ADMIN_IDS | DEBUG_ADMIN_IDS:
        try:
            await bot.send_message(admin_id, message)
        except Exception:
            logging.warning("Admin notification failed for %s", admin_id, exc_info=True)


async def _resolve_attempt(session, payment):
    payment_id = str(payment.id)
    attempt = (await session.execute(
        select(PaymentAttempt).where(PaymentAttempt.payment_id == payment_id).with_for_update()
    )).scalar_one_or_none()
    metadata = yk.payment_metadata(payment)
    metadata_order_id = metadata.get("order_id")
    metadata_attempt_id = metadata.get("payment_attempt_id")

    if attempt is None and str(metadata_attempt_id or "").isdigit():
        attempt = await session.get(PaymentAttempt, int(metadata_attempt_id), with_for_update=True)
        if attempt and attempt.payment_id not in (None, payment_id):
            raise ValueError("PaymentAttempt is already linked to another payment_id")
        if attempt and attempt.payment_id is None:
            attempt.payment_id = payment_id

    if attempt is None:
        return None
    if attempt.idempotency_key is not None and metadata_attempt_id is None:
        raise ValueError("New payment is missing metadata.payment_attempt_id")

    validate_payment_attempt(
        attempt_id=attempt.id, order_id=attempt.order_id,
        expected_amount=attempt.amount, expected_currency=attempt.currency,
        metadata={"order_id": metadata_order_id, "payment_attempt_id": metadata_attempt_id},
        actual_amount=yk.payment_amount(payment), actual_currency=str(payment.amount.currency),
    )
    account_id = yk.payment_account_id(payment)
    if account_id and YOOKASSA_SHOP_ID and account_id != str(YOOKASSA_SHOP_ID):
        raise ValueError("Payment recipient account does not match configured YooKassa shop")
    return attempt


async def _apply_payment(bot: Bot, payment) -> None:
    notification = None
    async with SessionMaker() as session:
        async with session.begin():
            metadata_order_id = yk.payment_metadata(payment).get("order_id")
            if not str(metadata_order_id or "").isdigit():
                raise ValueError("Payment metadata.order_id is missing or invalid")
            order = await session.get(Order, int(metadata_order_id), with_for_update=True)
            if order is None:
                raise ValueError(f"Order {metadata_order_id} does not exist")
            attempt = await _resolve_attempt(session, payment)
            if attempt is None:
                raise ValueError(f"No local PaymentAttempt for payment {payment.id}")
            if attempt.order_id != order.id:
                raise ValueError("PaymentAttempt belongs to another order")
            from decimal import Decimal
            if Decimal(str(order.amount)).quantize(Decimal("0.01")) != attempt.amount:
                raise ValueError("Order amount no longer matches the recorded PaymentAttempt amount")
            if payment.status == "succeeded" and bool(payment.paid):
                previous_success = (await session.execute(
                    select(PaymentAttempt.id).where(
                        PaymentAttempt.order_id == order.id,
                        PaymentAttempt.status == "succeeded",
                        PaymentAttempt.id != attempt.id,
                    ).limit(1)
                )).scalar_one_or_none()
                first_confirmation = attempt.status != "succeeded"
                attempt.status = "succeeded"
                if attempt.paid_at is None:
                    attempt.paid_at = datetime.now(timezone.utc)
                await track_paid_attempt(session, attempt.id, auto_email=first_confirmation)
                attempt.last_checked_at = datetime.now(timezone.utc)
                attempt.last_error = None
                order.status = "succeeded"
                await enqueue_delivery(session, order.id)
                if previous_success:
                    attempt.last_error = "POSSIBLE_OVERPAYMENT: another payment attempt for this order is already succeeded"
                    notification = (
                        f"⚠️ Возможная повторная оплата: заказ #{order.id}, "
                        f"дополнительный платёж {payment.id}. Выдача повторно не запускается."
                    )
            elif payment.status == "canceled":
                if attempt.status != "succeeded":
                    attempt.status = "canceled"
                    attempt.canceled_at = datetime.now(timezone.utc)
                    attempt.last_checked_at = datetime.now(timezone.utc)
                    active = (await session.execute(
                        select(PaymentAttempt.id).where(
                            PaymentAttempt.order_id == order.id,
                            PaymentAttempt.status.in_(("creating", "creation_unknown", "pending")),
                        ).limit(1)
                    )).scalar_one_or_none()
                    if active is None and order.status != "succeeded":
                        order.status = "canceled"
            else:
                attempt.last_checked_at = datetime.now(timezone.utc)
    if notification:
        await _notify_admins(bot, notification)


async def yookassa_webhook(request: web.Request) -> web.Response:
    if WEBHOOK_CHECK_IP and not _ip_allowed(_client_ip(request)):
        logging.warning("Rejected YooKassa webhook IP: %s", _client_ip(request))
        return web.Response(status=403)
    try:
        body = await request.json()
        event = body["event"]
        payment_id = str(body["object"]["id"])
    except (ValueError, KeyError, TypeError):
        return web.Response(status=400)
    if event not in {"payment.succeeded", "payment.canceled"}:
        return web.Response(status=200)
    try:
        payment = await yk.get_payment(payment_id)
        if event == "payment.succeeded" and not (payment.status == "succeeded" and payment.paid):
            logging.warning("Webhook event/status mismatch for payment %s", payment_id)
            return web.Response(status=200)
        if event == "payment.canceled" and payment.status != "canceled":
            logging.warning("Webhook canceled/status mismatch for payment %s", payment_id)
            return web.Response(status=200)
        await _apply_payment(request.app["bot"], payment)
    except ValueError as exc:
        logging.error("Rejected payment notification %s: %s", payment_id, exc)
        await _notify_admins(request.app["bot"], f"⚠️ Требуется проверка платежа {payment_id}: {exc}")
        return web.Response(status=200)
    except Exception:
        logging.exception("Webhook processing failed for payment %s", payment_id)
        return web.Response(status=500)
    return web.Response(status=200)


async def _reconcile_missing_payment_ids(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    async with SessionMaker() as session:
        attempts = (await session.execute(
            select(PaymentAttempt).where(
                PaymentAttempt.payment_id.is_(None),
                PaymentAttempt.status.in_(("creation_unknown", "reconciliation_required")),
                PaymentAttempt.created_at >= now - timedelta(days=7),
                (PaymentAttempt.last_checked_at.is_(None) |
                 (PaymentAttempt.last_checked_at < now - timedelta(minutes=10))),
            ).order_by(PaymentAttempt.created_at).limit(50)
        )).scalars().all()
        snapshots = [(a.id, a.order_id, a.amount, a.currency, a.created_at) for a in attempts]
        for attempt in attempts:
            attempt.last_checked_at = now
        if attempts:
            await session.commit()
    if not snapshots:
        return
    try:
        from_time = min(item[4] for item in snapshots) - timedelta(minutes=2)
        items = await yk.list_payments(from_time, now, limit=100)
    except Exception:
        logging.warning("Could not list YooKassa payments for lost-response reconciliation", exc_info=True)
        return

    by_attempt: dict[str, list] = {}
    for payment in items:
        metadata = yk.payment_metadata(payment)
        attempt_id = metadata.get("payment_attempt_id")
        if attempt_id is not None:
            by_attempt.setdefault(str(attempt_id), []).append(payment)

    for attempt_id, order_id, amount, currency, created_at in snapshots:
        matches = by_attempt.get(str(attempt_id), [])
        if len(matches) != 1:
            if len(matches) > 1:
                await _notify_admins(bot, f"⚠️ Найдено несколько платежей ЮKassa для PaymentAttempt #{attempt_id}; требуется ручная сверка.")
            continue
        payment = matches[0]
        metadata = yk.payment_metadata(payment)
        if str(metadata.get("order_id")) != str(order_id):
            continue
        if yk.payment_amount(payment) != amount or str(payment.amount.currency) != currency:
            await _notify_admins(bot, f"⚠️ Сумма/валюта платежа {payment.id} не совпадает с попыткой #{attempt_id}.")
            continue
        account_id = yk.payment_account_id(payment)
        if account_id and YOOKASSA_SHOP_ID and account_id != str(YOOKASSA_SHOP_ID):
            await _notify_admins(bot, f"⚠️ Получатель платежа {payment.id} не совпадает с магазином.")
            continue
        async with SessionMaker() as session:
            async with session.begin():
                attempt = await session.get(PaymentAttempt, attempt_id, with_for_update=True)
                if not attempt or attempt.payment_id is not None:
                    continue
                attempt.payment_id = str(payment.id)
                attempt.confirmation_url = yk.confirmation_url(payment)
                attempt.provider_metadata = metadata
                attempt.last_checked_at = now
                if payment.status == "pending":
                    attempt.status = "pending"
                elif payment.status == "succeeded" and payment.paid:
                    attempt.status = "creation_unknown"
                elif payment.status == "canceled":
                    attempt.status = "canceled"
                    attempt.canceled_at = now
        await _apply_payment(bot, payment)


async def _reconcile_loop(bot: Bot, interval: int) -> None:
    while True:
        try:
            await _reconcile_missing_payment_ids(bot)
            now = datetime.now(timezone.utc)
            async with SessionMaker() as session:
                attempts = (await session.execute(
                    select(PaymentAttempt.id, PaymentAttempt.payment_id).where(
                        PaymentAttempt.payment_id.is_not(None),
                        PaymentAttempt.status.in_(("pending", "creation_unknown", "reconciliation_required")),
                        (PaymentAttempt.last_checked_at.is_(None) |
                         (PaymentAttempt.last_checked_at < now - timedelta(minutes=1))),
                    ).order_by(PaymentAttempt.updated_at).limit(100)
                )).all()
                for attempt_id, _payment_id in attempts:
                    await session.execute(
                        update(PaymentAttempt).where(PaymentAttempt.id == attempt_id)
                        .values(last_checked_at=now)
                    )
                if attempts:
                    await session.commit()
            for attempt_id, payment_id in attempts:
                try:
                    payment = await yk.get_payment(payment_id)
                    await _apply_payment(bot, payment)
                except Exception:
                    logging.warning("Payment reconciliation failed for %s", payment_id, exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Payment reconciliation loop failed")
        await asyncio.sleep(interval)


def start_payment_watcher(bot: Bot, interval: int = 30) -> asyncio.Task | None:
    if not yk.is_configured():
        return None
    return asyncio.create_task(_reconcile_loop(bot, interval), name="payment-watcher")


async def start_webhook_server(bot: Bot) -> web.AppRunner | None:
    if not yk.is_configured():
        logging.warning("YooKassa is not configured; webhook server is disabled")
        return None
    app = web.Application(client_max_size=64 * 1024)
    app["bot"] = bot
    app.router.add_post(WEBHOOK_PATH, yookassa_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT).start()
    logging.info("YooKassa webhook listening on %s:%s%s", WEBHOOK_HOST, WEBHOOK_PORT, WEBHOOK_PATH)
    return runner
