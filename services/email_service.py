from __future__ import annotations

import logging
from email.message import EmailMessage
from email.utils import formatdate

from aiosmtplib import SMTP

from config import (
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_START_TLS,
    SMTP_TIMEOUT,
    SMTP_USE_TLS,
    SMTP_USER,
)

logger = logging.getLogger(__name__)


class SMTPDeliveryError(RuntimeError):

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        permanent: bool = False,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.permanent = permanent
        self.ambiguous = ambiguous


def smtp_config_error() -> str | None:
    if not all((SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM)):
        return "Не заданы SMTP_HOST, SMTP_USER, SMTP_PASSWORD или SMTP_FROM"
    if SMTP_USE_TLS and SMTP_START_TLS:
        return "SMTP_USE_TLS и SMTP_START_TLS нельзя включать одновременно"
    if SMTP_PORT == 465 and (not SMTP_USE_TLS or SMTP_START_TLS):
        return "Для SMTP-порта 465 требуется SMTP_USE_TLS=true и SMTP_START_TLS=false"
    if SMTP_PORT == 587 and (SMTP_USE_TLS or not SMTP_START_TLS):
        return "Для SMTP-порта 587 требуется SMTP_USE_TLS=false и SMTP_START_TLS=true"
    if SMTP_PORT <= 0 or SMTP_PORT > 65535:
        return "SMTP_PORT должен быть в диапазоне 1..65535"
    if SMTP_TIMEOUT <= 0:
        return "SMTP_TIMEOUT должен быть больше нуля"
    return None


def is_configured() -> bool:
    return smtp_config_error() is None


def _safe_error(exc: Exception) -> str:
    value = str(exc)
    if SMTP_PASSWORD:
        value = value.replace(SMTP_PASSWORD, "[REDACTED]")
    return value[:1500] or type(exc).__name__


def _refused_recipients(result: object) -> list[str]:
    if not isinstance(result, dict):
        return []
    refused: list[str] = []
    for address, response in result.items():
        code = getattr(response, "code", None)
        refused.append(f"recipient refused (SMTP code {code or 'unknown'})")
    return refused


async def send_email(
    *,
    email: str,
    subject: str,
    text: str,
    attachment: bytes,
    message_id: str,
    order_id: int,
) -> str:
    config_error = smtp_config_error()
    if config_error:
        raise SMTPDeliveryError("configuration", config_error, permanent=True)
    if not attachment or not attachment.startswith(b"%PDF-"):
        raise SMTPDeliveryError(
            "message", "Вложение отсутствует или не похоже на PDF", permanent=True
        )

    message = _message(email, subject, text, message_id, order_id)
    message.add_attachment(
        attachment,
        maintype="application",
        subtype="pdf",
        filename=f"order_{order_id}.pdf",
    )
    return await _send_message(message, order_id)


def _message(email: str, subject: str, text: str, message_id: str, order_id: int) -> EmailMessage:
    message = EmailMessage()
    message["From"] = SMTP_FROM
    message["To"] = email
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = message_id
    message["X-Order-ID"] = str(order_id)
    message.set_content(text)
    return message


async def send_receipt_email(
    *, email: str, text: str, message_id: str, order_id: int,
) -> str:
    config_error = smtp_config_error()
    if config_error:
        raise SMTPDeliveryError("configuration", config_error, permanent=True)
    message = _message(email, f"Реквизиты кассового чека по заказу №{order_id}", text, message_id, order_id)
    return await _send_message(message, order_id)


async def _send_message(message: EmailMessage, order_id: int) -> str:
    tls_mode = "implicit_tls" if SMTP_USE_TLS else "starttls" if SMTP_START_TLS else "plain"
    smtp = SMTP(
        hostname=SMTP_HOST,
        port=SMTP_PORT,
        username=None,
        password=None,
        use_tls=SMTP_USE_TLS,
        start_tls=SMTP_START_TLS,
        timeout=SMTP_TIMEOUT,
    )
    accepted = False
    logger.info(
        "SMTP connect starting order_id=%s host=%s port=%s tls_mode=%s timeout=%ss",
        order_id, SMTP_HOST, SMTP_PORT, tls_mode, SMTP_TIMEOUT,
    )
    try:
        try:
            # aiosmtplib.connect() waits for the server greeting, sends EHLO,
            # and negotiates STARTTLS when configured.
            await smtp.connect()
        except Exception as exc:
            safe = _safe_error(exc)
            logger.warning(
                "SMTP connection/greeting failed order_id=%s error_type=%s error=%s",
                order_id, type(exc).__name__, safe,
            )
            raise SMTPDeliveryError(
                "connect", f"SMTP connection or server greeting failed: {safe}"
            ) from exc

        logger.info(
            "SMTP server greeting received; connection and TLS negotiation complete order_id=%s",
            order_id,
        )

        try:
            await smtp.login(SMTP_USER, SMTP_PASSWORD)
        except Exception as exc:
            safe = _safe_error(exc)
            logger.error(
                "SMTP authentication failed order_id=%s error_type=%s error=%s",
                order_id, type(exc).__name__, safe,
            )
            raise SMTPDeliveryError(
                "authentication", f"SMTP authentication failed: {safe}", permanent=True
            ) from exc

        logger.info("SMTP authentication succeeded order_id=%s", order_id)
        logger.info(
            "SMTP message transfer starting order_id=%s message_id=%s",
            order_id, message["Message-ID"],
        )
        try:
            refused_map, _server_response = await smtp.send_message(message)
        except Exception as exc:
            safe = _safe_error(exc)
            logger.error(
                "SMTP message transfer failed order_id=%s error_type=%s error=%s",
                order_id, type(exc).__name__, safe,
            )
            # Explicit SMTP 4xx/5xx rejections happen before acceptance; a lost
            # connection during DATA is ambiguous and must not be called success.
            response_code = getattr(exc, "code", None)
            is_explicit_rejection = (
                type(exc).__name__ in {
                    "SMTPRecipientsRefused", "SMTPRecipientRefused",
                    "SMTPSenderRefused", "SMTPDataError", "SMTPResponseException",
                }
                and isinstance(response_code, int)
            )
            raise SMTPDeliveryError(
                "send",
                f"SMTP message transfer failed: {safe}",
                permanent=is_explicit_rejection and 500 <= response_code < 600,
                ambiguous=not is_explicit_rejection,
            ) from exc

        refused = _refused_recipients(refused_map)
        if refused:
            details = "; ".join(refused)
            codes = [getattr(value, "code", None) for value in refused_map.values()]
            permanent = bool(codes) and all(isinstance(code, int) and 500 <= code < 600 for code in codes)
            logger.error("SMTP server refused recipient order_id=%s %s", order_id, details)
            raise SMTPDeliveryError(
                "send", details, permanent=permanent, ambiguous=False
            )

        accepted = True
        logger.info("SMTP server accepted message order_id=%s message_id=%s", order_id, message["Message-ID"])
        return "SMTP server accepted message"
    finally:
        # A QUIT failure after DATA was accepted must not turn a successful send
        # into a retry and create a duplicate email.
        try:
            if getattr(smtp, "is_connected", False):
                await smtp.quit()
        except Exception as exc:
            logger.warning(
                "SMTP QUIT failed after message_accepted=%s order_id=%s error_type=%s",
                accepted, order_id, type(exc).__name__,
            )
        finally:
            try:
                smtp.close()
            except Exception:
                logger.debug("SMTP socket close raised order_id=%s", order_id, exc_info=True)
