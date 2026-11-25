import asyncio
import io
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError, TelegramNetworkError, TelegramForbiddenError,
    TelegramBadRequest, TelegramRetryAfter,
)
from aiogram.types import BufferedInputFile
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import (
    ADMIN_IDS, DEBUG_ADMIN_IDS, DELIVERY_LEASE_SECONDS,
    DELIVERY_MAX_BACKOFF_HOURS, DELIVERY_POLL_SECONDS,
)
from services.payment_rules import delivery_retry_delay
from db.base import SessionMaker
from db.models import DeliveryAttempt, DeliveryTask, Order, PaymentAttempt, Product, User
from services.email_service import SMTPDeliveryError, send_email, smtp_config_error
from services.pdf_service import add_watermark


@dataclass
class ClaimedTask:
    task_id: int
    order_id: int
    channel: str
    attempt_id: int
    attempt_number: int
    lease_token: str


class PermanentDeliveryError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _retry_delay(attempt_number: int) -> timedelta:
    return delivery_retry_delay(attempt_number, DELIVERY_MAX_BACKOFF_HOURS)


async def enqueue_delivery(session, order_id: int) -> None:
    for channel in ("telegram", "email"):
        stmt = pg_insert(DeliveryTask).values(
            order_id=order_id, channel=channel, status="pending", attempts=0,
            next_retry_at=_now(), created_at=_now(), updated_at=_now(),
        ).on_conflict_do_nothing(index_elements=["order_id", "channel"])
        await session.execute(stmt)


async def _recover_expired(conn, now: datetime) -> None:
    expired = (await conn.execute(
        select(DeliveryTask).where(
            DeliveryTask.status == "processing",
            DeliveryTask.locked_until < now,
        ).with_for_update(skip_locked=True)
    )).scalars().all()
    for task in expired:
        started = (await conn.execute(
            select(DeliveryAttempt).where(
                DeliveryAttempt.task_id == task.id,
                DeliveryAttempt.status == "started",
            ).order_by(DeliveryAttempt.attempt_number.desc()).limit(1).with_for_update()
        )).scalar_one_or_none()
        if started:
            started.status = "unknown"
            started.finished_at = now
            started.error_type = "WorkerLeaseExpired"
            started.error_message = "Worker lease expired; external delivery result is unknown"
        task.status = "retry_wait"
        task.next_retry_at = now
        task.locked_until = None
        task.locked_by = None
        task.lease_token = None
        task.last_error = "Истёк срок блокировки worker; результат предыдущей отправки неизвестен"


async def claim_next_task(worker_id: str) -> ClaimedTask | None:
    now = _now()
    async with SessionMaker() as session:
        async with session.begin():
            await _recover_expired(session, now)
            task = (await session.execute(
                select(DeliveryTask).where(
                    DeliveryTask.status.in_(("pending", "retry_wait")),
                    DeliveryTask.next_retry_at <= now,
                ).order_by(DeliveryTask.next_retry_at, DeliveryTask.id)
                .limit(1).with_for_update(skip_locked=True)
            )).scalar_one_or_none()
            if task is None:
                return None
            token = str(uuid.uuid4())
            task.status = "processing"
            task.attempts += 1
            task.locked_by = worker_id
            task.lease_token = token
            task.locked_until = now + timedelta(seconds=DELIVERY_LEASE_SECONDS)
            task.updated_at = now
            attempt = DeliveryAttempt(
                task_id=task.id, attempt_number=task.attempts,
                status="started", started_at=now,
            )
            session.add(attempt)
            await session.flush()
            return ClaimedTask(task.id, task.order_id, task.channel, attempt.id,
                               task.attempts, token)


async def _load_delivery_data(order_id: int):
    async with SessionMaker() as session:
        order = await session.get(Order, order_id)
        if not order:
            raise PermanentDeliveryError(f"Заказ #{order_id} не найден")
        paid = (await session.execute(
            select(PaymentAttempt.id).where(
                PaymentAttempt.order_id == order_id,
                PaymentAttempt.status == "succeeded",
            ).limit(1)
        )).scalar_one_or_none()
        if paid is None:
            raise PermanentDeliveryError("Оплата заказа не подтверждена в PaymentAttempt")
        user = await session.get(User, order.user_id)
        product = await session.get(Product, order.product_id)
        if not user or not product:
            raise PermanentDeliveryError("Не найден покупатель или товар")
        if not product.file_id:
            raise PermanentDeliveryError("У товара отсутствует Telegram file_id")
        if not order.secret_code:
            candidate = secrets.token_urlsafe(12)
            await session.execute(
                update(Order).where(Order.id == order.id, Order.secret_code.is_(None))
                .values(secret_code=candidate)
            )
            await session.refresh(order)
            await session.commit()
        return {
            "telegram_id": order.customer_telegram_id or user.telegram_id,
            "fio": order.customer_fio or user.fio or f"Покупатель {user.telegram_id}",
            "email": order.customer_email or user.email,
            "title": product.title,
            "file_id": product.file_id,
            "secret_code": order.secret_code,
        }


async def _build_pdf(bot: Bot, data: dict) -> bytes:
    try:
        tg_file = await bot.get_file(data["file_id"])
        if not tg_file.file_path:
            raise PermanentDeliveryError("Telegram не вернул путь к исходному PDF")
        source = io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=source)
        source.seek(0)
        try:
            result = await asyncio.to_thread(
                add_watermark, source, data["fio"], data["email"] or "Email не указан",
                data["secret_code"],
            )
        except Exception as exc:
            raise PermanentDeliveryError(f"Не удалось обработать исходный PDF: {exc}") from exc
        return result.getvalue()
    except PermanentDeliveryError:
        raise
    except TelegramNetworkError:
        raise
    except TelegramAPIError as exc:
        raise PermanentDeliveryError(f"Telegram не смог получить исходный PDF: {exc}") from exc
    except (ValueError, OSError) as exc:
        raise PermanentDeliveryError(f"Исходный PDF повреждён или недоступен: {exc}") from exc
    except Exception:
        logging.exception("PDF download/generation failed")
        raise


async def _send_channel(bot: Bot, claim: ClaimedTask) -> tuple[str, str | None, dict | None]:
    data = await _load_delivery_data(claim.order_id)
    if claim.channel == "email" and not data["email"]:
        return "skipped", None, {"reason": "Email не указан; канал необязательный"}
    if claim.channel == "email":
        config_error = smtp_config_error()
        if config_error:
            raise PermanentDeliveryError(f"SMTP configuration error: {config_error}")
    pdf = await _build_pdf(bot, data)
    if claim.channel == "telegram":
        message = await bot.send_document(
            chat_id=data["telegram_id"],
            document=BufferedInputFile(pdf, filename=f"order_{claim.order_id}.pdf"),
            caption=f"📦 Ваш заказ #{claim.order_id}: {data['title']}",
        )
        return "accepted", str(message.message_id), {"chat_id": data["telegram_id"]}
    if claim.channel == "email":
        message_id = f"<order-{claim.order_id}@hsskkaa.ru>"
        response = await send_email(
            email=data["email"], subject=f"Ваш заказ #{claim.order_id}: {data['title']}",
            text=(f"Здравствуйте, {data['fio']}!\n\nСпасибо за покупку «{data['title']}».\n"
                  f"Ваш PDF находится во вложении.\nКод экземпляра: {data['secret_code']}\n\n"
                  "Данное сообщение создано автоматически, отвечать на него не нужно."),
            attachment=pdf, message_id=message_id, order_id=claim.order_id,
        )
        return "accepted", message_id, {"smtp_response": response[:500]}
    raise PermanentDeliveryError(f"Неизвестный канал доставки: {claim.channel}")


async def _finish(claim: ClaimedTask, status: str, *, external_id: str | None = None,
                  error: Exception | None = None, metadata: dict | None = None) -> bool:
    now = _now()
    async with SessionMaker() as session:
        async with session.begin():
            task = (await session.execute(
                select(DeliveryTask).where(
                    DeliveryTask.id == claim.task_id,
                    DeliveryTask.status == "processing",
                    DeliveryTask.lease_token == claim.lease_token,
                ).with_for_update()
            )).scalar_one_or_none()
            if task is None:
                return False
            attempt = await session.get(DeliveryAttempt, claim.attempt_id)
            if attempt is None or attempt.status != "started":
                raise RuntimeError("DeliveryAttempt missing or already finalized")
            attempt.status = (
                "accepted" if status == "accepted"
                else "unknown" if status == "unknown"
                else "failed"
            )
            attempt.finished_at = now
            attempt.external_message_id = external_id
            attempt.result_metadata = metadata
            if error:
                attempt.error_type = type(error).__name__
                attempt.error_message = str(error)[:4000]
                task.last_error = f"{type(error).__name__}: {str(error)[:1500]}"
            if status == "accepted":
                task.status = "delivered"
                task.delivered_at = now
                task.next_retry_at = None
                task.last_error = None
            elif status == "skipped":
                task.status = "skipped"
                task.next_retry_at = None
            elif status == "failed_permanent":
                task.status = "blocked"
                task.next_retry_at = None
            else:
                task.status = "retry_wait"
                delay = _retry_delay(claim.attempt_number)
                if isinstance(error, TelegramRetryAfter):
                    delay = max(delay, timedelta(seconds=float(error.retry_after)))
                task.next_retry_at = now + delay
            task.locked_until = None
            task.locked_by = None
            task.lease_token = None
            task.updated_at = now
            return True


async def _heartbeat(claim: ClaimedTask) -> None:
    while True:
        await asyncio.sleep(max(30, DELIVERY_LEASE_SECONDS // 3))
        try:
            async with SessionMaker() as session:
                result = await session.execute(
                    update(DeliveryTask)
                    .where(DeliveryTask.id == claim.task_id,
                           DeliveryTask.status == "processing",
                           DeliveryTask.lease_token == claim.lease_token)
                    .values(locked_until=_now() + timedelta(seconds=DELIVERY_LEASE_SECONDS))
                )
                await session.commit()
                if not result.rowcount:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Could not extend delivery lease for task %s", claim.task_id)


async def process_claim(bot: Bot, claim: ClaimedTask) -> None:
    heartbeat = asyncio.create_task(_heartbeat(claim), name=f"lease-{claim.task_id}")
    try:
        result, external_id, metadata = await _send_channel(bot, claim)
        await _finish(claim, result, external_id=external_id, metadata=metadata)
    except SMTPDeliveryError as exc:
        logging.error(
            "SMTP delivery failure task=%s order=%s stage=%s permanent=%s ambiguous=%s: %s",
            claim.task_id, claim.order_id, exc.stage, exc.permanent, exc.ambiguous, str(exc),
        )
        if exc.permanent:
            await _finish(claim, "failed_permanent", error=exc)
            for admin_id in ADMIN_IDS | DEBUG_ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        f"⚠️ Email-доставка заблокирована: заказ #{claim.order_id}. "
                        f"Этап: {exc.stage}. Проверьте SMTP-настройки.\n{str(exc)[:900]}",
                    )
                except Exception:
                    logging.warning("Could not notify admin %s about SMTP failure", admin_id)
        elif exc.ambiguous:
            await _finish(claim, "unknown", error=exc)
        else:
            await _finish(claim, "retryable", error=exc)
    except PermanentDeliveryError as exc:
        logging.error("Permanent delivery failure task=%s: %s", claim.task_id, exc)
        await _finish(claim, "failed_permanent", error=exc)
        for admin_id in ADMIN_IDS | DEBUG_ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"⚠️ Доставка заблокирована: заказ #{claim.order_id}, канал {claim.channel}.\n{type(exc).__name__}: {str(exc)[:1200]}",
                )
            except Exception:
                logging.warning("Could not notify admin %s about blocked delivery", admin_id)
    except (TelegramNetworkError, TimeoutError, asyncio.TimeoutError) as exc:
        logging.warning("Ambiguous delivery result task=%s", claim.task_id, exc_info=True)
        await _finish(claim, "unknown", error=exc)
    except TelegramRetryAfter as exc:
        await _finish(claim, "unknown", error=exc)
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        await _finish(claim, "failed_permanent", error=exc)
    except Exception as exc:
        logging.exception("Delivery attempt failed task=%s", claim.task_id)
        await _finish(claim, "unknown", error=exc)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def force_retry_task(task_id: int) -> bool:
    async with SessionMaker() as session:
        async with session.begin():
            task = (await session.execute(
                select(DeliveryTask).where(DeliveryTask.id == task_id).with_for_update()
            )).scalar_one_or_none()
            if task is None or task.status in {"delivered", "processing"}:
                return False
            paid = (await session.execute(
                select(PaymentAttempt.id).where(
                    PaymentAttempt.order_id == task.order_id,
                    PaymentAttempt.status == "succeeded",
                ).limit(1)
            )).scalar_one_or_none()
            if paid is None:
                return False
            task.status = "pending"
            task.next_retry_at = _now()
            task.locked_until = None
            task.locked_by = None
            task.lease_token = None
            task.last_error = "Повторная отправка запрошена администратором"
            return True


async def delivery_worker(bot: Bot, worker_id: str) -> None:
    logging.info("Delivery worker %s started", worker_id)
    while True:
        try:
            claim = await claim_next_task(worker_id)
            if claim is None:
                await asyncio.sleep(DELIVERY_POLL_SECONDS)
                continue
            await process_claim(bot, claim)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Delivery worker %s loop error", worker_id)
            await asyncio.sleep(3)


async def deliver_order(order_id: int, bot: Bot) -> None:
    async with SessionMaker() as session:
        async with session.begin():
            await enqueue_delivery(session, order_id)
