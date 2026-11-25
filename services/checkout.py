import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession

import keyboards as kb
import texts
from config import YOOKASSA_IDEMPOTENCY_SAFE_HOURS, YOOKASSA_RETURN_URL
from db.models import Order, PaymentAttempt, Product, User
from services import yookassa_service as yk
from services.payment_rules import can_retry_idempotent_create, validate_payment_attempt

PAYMENT_ERROR = (
    "⚠️ <b>Не удалось безопасно подтвердить создание платежа.</b>\n"
    "Не создавайте оплату повторно через сторонние ссылки. Попробуйте открыть оплату позже "
    "или обратитесь в поддержку."
)


def _redirect(product: Product, order: Order, user: User) -> str:
    return texts.redirect(
        product,
        order.customer_fio or user.fio or "",
        order.customer_email or user.email or "",
        order.id,
    )


async def _locked_pending_order(session: AsyncSession, user: User, product: Product) -> Order:
    # Serialize checkout for the same user/product across application instances.
    lock_name = f"checkout:{user.id}:{product.id}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": lock_name},
    )
    order = (await session.execute(
        select(Order).where(
            Order.user_id == user.id,
            Order.product_id == product.id,
            Order.status == "pending",
        ).order_by(Order.id.desc()).with_for_update()
    )).scalars().first()
    if order is None:
        order = Order(
            user_id=user.id,
            product_id=product.id,
            amount=product.price,
            status="pending",
            customer_telegram_id=user.telegram_id,
            customer_fio=user.fio,
            customer_email=user.email,
        )
        session.add(order)
        await session.flush()
    else:
        has_attempt = (await session.execute(
            select(PaymentAttempt.id).where(PaymentAttempt.order_id == order.id).limit(1)
        )).scalar_one_or_none()
        if has_attempt is None:
            order.amount = product.price
            order.customer_telegram_id = user.telegram_id
            order.customer_fio = user.fio
            order.customer_email = user.email
    return order


async def _refresh_known_payment(
    session: AsyncSession,
    bot: Bot,
    order_id: int,
    attempt_id: int,
    payment_id: str,
    product: Product,
    user: User,
):
    await session.commit()
    try:
        remote = await yk.get_payment(payment_id)
        from webhook_server import _apply_payment
        await _apply_payment(bot, remote)
    except Exception:
        logging.warning("Could not reconcile existing payment %s", payment_id, exc_info=True)
        return PAYMENT_ERROR, kb.back_to_catalog(), False

    if remote.status == "succeeded" and bool(remote.paid):
        return (
            f"✅ Оплата заказа №{order_id} уже подтверждена.\n"
            "Система проверяет доставку. Повторный платёж создавать не нужно.",
            kb.back_to_catalog(),
            True,
        )

    if remote.status == "canceled":
        return None, None, True

    if remote.status == "pending":
        url = yk.confirmation_url(remote)
        if not url:
            return PAYMENT_ERROR, kb.back_to_catalog(), False
        # Lock in the same order as webhook/checkout: Order first, then PaymentAttempt.
        async with session.begin():
            await session.execute(
                select(Order).where(Order.id == order_id).with_for_update()
            )
            attempt = (await session.execute(
                select(PaymentAttempt).where(
                    PaymentAttempt.id == attempt_id,
                    PaymentAttempt.order_id == order_id,
                ).with_for_update()
            )).scalar_one_or_none()
            if attempt is None:
                return PAYMENT_ERROR, kb.back_to_catalog(), False
            if attempt.payment_id not in (None, payment_id):
                return PAYMENT_ERROR, kb.back_to_catalog(), False
            if attempt.status == "succeeded":
                return (
                    f"✅ Оплата заказа №{order_id} уже подтверждена.\nСистема проверяет доставку.",
                    kb.back_to_catalog(), True,
                )
            attempt.payment_id = payment_id
            attempt.confirmation_url = url
            attempt.status = "pending"
            attempt.provider_metadata = yk.payment_metadata(remote)
            attempt.last_checked_at = datetime.now(timezone.utc)
        order = await session.get(Order, order_id)
        return _redirect(product, order, user), _pay_markup(url), True

    return PAYMENT_ERROR, kb.back_to_catalog(), False


async def prepare_checkout(
    session: AsyncSession,
    user: User,
    product: Product,
    bot: Bot,
) -> tuple[str, InlineKeyboardMarkup]:
    if not user.is_registered or not user.email:
        return "Сначала завершите регистрацию и укажите Email.", kb.back_to_catalog()
    if not yk.is_configured():
        return PAYMENT_ERROR, kb.back_to_catalog()

    unresolved_legacy = (await session.execute(
        select(Order.id).where(
            Order.user_id == user.id,
            Order.product_id == product.id,
            Order.status == "succeeded",
            ~exists(select(PaymentAttempt.id).where(PaymentAttempt.order_id == Order.id)),
        ).limit(1)
    )).scalar_one_or_none()
    unresolved_attempt = (await session.execute(
        select(PaymentAttempt.id).join(Order, Order.id == PaymentAttempt.order_id).where(
            Order.user_id == user.id,
            Order.product_id == product.id,
            PaymentAttempt.status == "reconciliation_required",
        ).limit(1)
    )).scalar_one_or_none()
    if unresolved_legacy or unresolved_attempt:
        return (
            "⚠️ Для этого товара есть старый заказ с неподтверждённым состоянием оплаты. "
            "Новая оплата временно отключена до ручной сверки администратором.",
            kb.back_to_catalog(),
        )

    return_url = YOOKASSA_RETURN_URL
    if not return_url:
        return_url = f"https://t.me/{(await bot.me()).username}"

    order = await _locked_pending_order(session, user, product)
    attempts = (await session.execute(
        select(PaymentAttempt).where(PaymentAttempt.order_id == order.id)
        .order_by(PaymentAttempt.id.desc()).with_for_update()
    )).scalars().all()
    if any(a.status == "succeeded" for a in attempts):
        await session.commit()
        return (
            f"✅ Оплата заказа №{order.id} уже подтверждена. Повторный платёж не требуется.",
            kb.back_to_catalog(),
        )

    active = next(
        (a for a in attempts if a.status in {"creating", "creation_unknown", "pending"}),
        None,
    )

    unresolved = next(
        (a for a in attempts if a.status == "reconciliation_required"),
        None,
    )
    if unresolved:
        await session.commit()
        return PAYMENT_ERROR, kb.back_to_catalog()

    if active and active.payment_id:
        result_text, markup, resolved = await _refresh_known_payment(
            session, bot, order.id, active.id, active.payment_id, product, user,
        )
        if result_text is not None:
            return result_text, markup
        if resolved:
            return await prepare_checkout(session, user, product, bot)
        return PAYMENT_ERROR, kb.back_to_catalog()

    if active and active.status == "pending":
        await session.commit()
        return PAYMENT_ERROR, kb.back_to_catalog()

    now = datetime.now(timezone.utc)
    if active and active.status in {"creating", "creation_unknown"}:
        if not active.idempotency_key or not active.request_payload:
            active.status = "reconciliation_required"
            active.last_error = (
                "Missing persisted idempotency key or request payload; "
                "refusing to create another payment"
            )
            await session.commit()
            return PAYMENT_ERROR, kb.back_to_catalog()
        if not can_retry_idempotent_create(
            active.created_at, now, YOOKASSA_IDEMPOTENCY_SAFE_HOURS
        ):
            active.status = "reconciliation_required"
            active.last_error = (
                "Idempotency retention safety window expired; manual reconciliation required"
            )
            await session.commit()
            return PAYMENT_ERROR, kb.back_to_catalog()
        attempt = active
    else:
        order.amount = product.price

        attempt = PaymentAttempt(
            order_id=order.id,
            idempotency_key=str(uuid.uuid4()),
            status="creating",
            amount=Decimal(str(product.price)).quantize(Decimal("0.01")),
            currency="RUB",
        )
        session.add(attempt)
        await session.flush()

    if not attempt.request_payload:
        if active is not None:
            attempt.status = "reconciliation_required"
            attempt.last_error = "Existing payment attempt has no persisted request payload"
            await session.commit()
            return PAYMENT_ERROR, kb.back_to_catalog()
        attempt.request_payload = yk.build_payload(
            amount=attempt.amount,
            order_id=order.id,
            attempt_id=attempt.id,
            email=order.customer_email or user.email,
            description=product.title,
            return_url=return_url,
        )

    payload = attempt.request_payload
    idempotency_key = attempt.idempotency_key
    attempt_id = attempt.id
    order_id = order.id
    redirect_text = _redirect(product, order, user)

    await session.commit()

    try:
        payment = await yk.create_payment(payload, idempotency_key)
        payment_id = str(payment.id)
        validate_payment_attempt(
            attempt_id=attempt_id, order_id=order_id,
            expected_amount=attempt.amount, expected_currency=attempt.currency,
            metadata=yk.payment_metadata(payment),
            actual_amount=yk.payment_amount(payment),
            actual_currency=str(payment.amount.currency),
        )
        account_id = yk.payment_account_id(payment)
        from config import YOOKASSA_SHOP_ID
        if account_id and YOOKASSA_SHOP_ID and account_id != str(YOOKASSA_SHOP_ID):
            raise ValueError("YooKassa payment recipient does not match configured shop")
        url = yk.confirmation_url(payment)
        if not url:
            raise RuntimeError(
                f"ЮKassa не вернула confirmation_url для платежа {payment_id}"
            )
        async with session.begin():
            await session.execute(
                select(Order).where(Order.id == order_id).with_for_update()
            )
            attempt = (await session.execute(
                select(PaymentAttempt).where(
                    PaymentAttempt.id == attempt_id,
                    PaymentAttempt.order_id == order_id,
                ).with_for_update()
            )).scalar_one_or_none()
            if attempt is None:
                raise RuntimeError("PaymentAttempt disappeared after YooKassa response")
            if attempt.payment_id not in (None, payment_id):
                raise RuntimeError("PaymentAttempt is already linked to another payment_id")
            if attempt.status != "succeeded":
                attempt.payment_id = payment_id
                attempt.confirmation_url = url
                attempt.status = "pending"
                attempt.provider_metadata = yk.payment_metadata(payment)
                attempt.last_error = None
        return redirect_text, _pay_markup(url)
    except Exception as exc:
        logging.exception("YooKassa create payment outcome uncertain for order %s", order_id)
        async with session.begin():
            await session.execute(
                select(Order).where(Order.id == order_id).with_for_update()
            )
            attempt = (await session.execute(
                select(PaymentAttempt).where(
                    PaymentAttempt.id == attempt_id,
                    PaymentAttempt.order_id == order_id,
                ).with_for_update()
            )).scalar_one_or_none()
            if attempt and attempt.status != "succeeded" and not attempt.payment_id:
                attempt.status = "creation_unknown"
                attempt.last_error = f"{type(exc).__name__}: {str(exc)[:1500]}"
        return PAYMENT_ERROR, kb.back_to_catalog()

def _pay_markup(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💳 Перейти к оплате",
                url=url,
            )
        ],
        [
            InlineKeyboardButton(
                text="🛠 Техподдержка",
                callback_data="support",
            )
        ],
        *kb.back_to_catalog().inline_keyboard,
    ])
