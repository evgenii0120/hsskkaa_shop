import os
from pathlib import Path
from sqlalchemy.engine import URL
from dotenv import load_dotenv
import logging

load_dotenv(
    dotenv_path=Path(__file__).resolve().parent / ".env",
    override=True,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SHOP_NAME = "hsskkaa"
OFERTA_URL = "https://hsskkaa.ru/p/oferta/"
PRIVACY_URL = "https://hsskkaa.ru/p/privacy/"


def _ids(name: str) -> set[int]:
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x.isdigit()}


ADMIN_IDS = _ids("ADMIN_IDS")
DEBUG_ADMIN_IDS = _ids("DEBUG_ADMIN_IDS")

DB_HOST = os.getenv("DB_HOST", "127.0.0.1").strip()
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER", "hsskkaa_shop").strip()
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "hsskkaa_shop").strip()
DATABASE_URL = URL.create(
    "postgresql+asyncpg", username=DB_USER, password=DB_PASSWORD,
    host=DB_HOST, port=DB_PORT, database=DB_NAME,
).render_as_string(hide_password=False)

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "false").lower() == "true"
SMTP_START_TLS = os.getenv("SMTP_START_TLS", "true").lower() == "true"
logging.getLogger(__name__).info(
    "SMTP config: host=%s port=%s use_tls=%s start_tls=%s",
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USE_TLS,
    SMTP_START_TLS,
)
SMTP_TIMEOUT = float(os.getenv("SMTP_TIMEOUT", "30"))


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "").strip()
YOOKASSA_SEND_RECEIPT = _bool("YOOKASSA_SEND_RECEIPT", "true")
YOOKASSA_VAT_CODE = int(os.getenv("YOOKASSA_VAT_CODE", "1"))
_tax = os.getenv("YOOKASSA_TAX_SYSTEM_CODE", "").strip()
YOOKASSA_TAX_SYSTEM_CODE = int(_tax) if _tax.isdigit() else None
YOOKASSA_IDEMPOTENCY_SAFE_HOURS = min(23, max(1, int(os.getenv("YOOKASSA_IDEMPOTENCY_SAFE_HOURS", "23"))))
PDF_FONT_PATH = os.getenv("PDF_FONT_PATH", "").strip()

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "127.0.0.1").strip()
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/yookassa/webhook").strip()
WEBHOOK_CHECK_IP = _bool("WEBHOOK_CHECK_IP", "true")
WEBHOOK_TRUST_PROXY = _bool("WEBHOOK_TRUST_PROXY", "false")

DELIVERY_WORKER_COUNT = min(8, max(1, int(os.getenv("DELIVERY_WORKER_COUNT", "2"))))
DELIVERY_POLL_SECONDS = max(1, float(os.getenv("DELIVERY_POLL_SECONDS", "3")))
DELIVERY_LEASE_SECONDS = max(120, int(os.getenv("DELIVERY_LEASE_SECONDS", "600")))
DELIVERY_MAX_BACKOFF_HOURS = min(6, max(1, int(os.getenv("DELIVERY_MAX_BACKOFF_HOURS", "6"))))
