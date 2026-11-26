import asyncio
import io
import logging
from datetime import timedelta, timezone
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import DEBUG_ADMIN_IDS, YOOKASSA_SHOP_ID
from db.models import ManualReceipt, Order, PaymentAttempt, Product
from handlers.common import edit_or_answer
from services import yookassa_service as yk
from services.manual_receipts import (
    MAX_RECEIPT_BYTES, STATUS_LABELS, enqueue_receipt, queue_receipt_retry, store_receipt_pdf,
)

router = Router()
router.callback_query.filter(F.from_user.id.in_(DEBUG_ADMIN_IDS), F.message.chat.type == "private")
router.message.filter(F.from_user.id.in_(DEBUG_ADMIN_IDS), F.chat.type == "private")
logger = logging.getLogger(__name__)
PAGE_SIZE = 12
MOSCOW = timezone(timedelta(hours=3))


class ReceiptUpload(StatesGroup):
    pdf = State()


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _keyboard(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _last_number(cb: CallbackQuery) -> int:
    return int(cb.data.rsplit(":", 1)[-1])


@router.callback_query(F.data.regexp(r"^receipts:(queue|old|sent):[0-9]{1,6}$"))
async def receipt_list(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    section = cb.data.split(":")[1]
    page = _last_number(cb)
    if section in {"queue", "sent"}:
        query = select(PaymentAttempt, ManualReceipt.status).join(
            ManualReceipt, ManualReceipt.attempt_id == PaymentAttempt.id
        )
        if section == "queue":
            query = query.where(ManualReceipt.status != "sent")
            header = "Требуют чеков"
            empty = "Все чеки отправлены или новых оплат пока нет."
        else:
            query = query.where(ManualReceipt.status == "sent")
            header = "Отправленные чеки"
            empty = "Отправленных чеков пока нет."
    else:
        known = exists(select(ManualReceipt.attempt_id).where(ManualReceipt.attempt_id == PaymentAttempt.id))
        query = (
            select(PaymentAttempt, Product.title)
            .select_from(PaymentAttempt)
            .join(Order, Order.id == PaymentAttempt.order_id)
            .join(Product, Product.id == Order.product_id)
            .where(
                PaymentAttempt.status == "succeeded", PaymentAttempt.payment_id.is_not(None),
                PaymentAttempt.amount > 0, ~known,
            )
        )
        header = "Ранее оплаченные заказы"
        empty = "Платежей на этой странице нет."
    entries = (await session.execute(
        query.order_by(PaymentAttempt.id.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE + 1)
    )).all()
    rows = []
    for attempt, detail in entries[:PAGE_SIZE]:
        label = detail if section == "old" else STATUS_LABELS.get(detail, detail)
        action = "legacy" if section == "old" else "open"
        rows.append([_button(
            f"№{attempt.order_id} · {attempt.amount:g} ₽ · {label[:40]}",
            f"receipts:{action}:{attempt.id}",
        )])
    navigation = []
    if page:
        navigation.append(_button("← Назад", f"receipts:{section}:{page - 1}"))
    if len(entries) > PAGE_SIZE:
        navigation.append(_button("Дальше →", f"receipts:{section}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    if section == "queue":
        rows.append([_button("Отправленные чеки", "receipts:sent:0")])
        rows.append([_button("Ранее оплаченные заказы", "receipts:old:0")])
    else:
        rows.append([_button("К очереди чеков", "receipts:queue:0")])
    rows.append([_button("В меню /debug", "god:panel")])
    hint = "Выберите платёж." if entries else empty
    await edit_or_answer(cb, f"<b>{header}</b>\n\n{hint}", _keyboard(rows))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^receipts:legacy:[0-9]{1,10}$"))
async def open_legacy(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    attempt = await session.get(PaymentAttempt, _last_number(cb))
    if not attempt or attempt.status != "succeeded" or not attempt.payment_id:
        await cb.answer("Подтверждённый платёж не найден.", show_alert=True)
        return
    await edit_or_answer(cb,
        f"<b>Заказ №{attempt.order_id} · {attempt.amount:g} ₽</b>\n\n"
        "Проверьте этот платёж в «Моём налоге». Если чек уже есть, используйте его PDF.",
        _keyboard([
            [_button("Добавить в очередь чеков", f"receipts:import:{attempt.id}")],
            [_button("Назад", "receipts:old:0")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.regexp(r"^receipts:import:[0-9]{1,10}$"))
async def import_legacy(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    attempt = await session.get(PaymentAttempt, _last_number(cb))
    if not attempt or attempt.status != "succeeded" or not attempt.payment_id or attempt.amount <= 0:
        await cb.answer("Этот платёж нельзя добавить.", show_alert=True)
        return
    order = await session.get(Order, attempt.order_id)
    try:
        payment = await asyncio.wait_for(yk.get_payment(attempt.payment_id), timeout=15)
        received = yk.payment_metadata(payment)
        if (str(payment.id) != attempt.payment_id or payment.test or not payment.paid or
                payment.status != "succeeded" or yk.payment_amount(payment) != attempt.amount or
                str(payment.amount.currency) != attempt.currency or
                str(received.get("order_id", order.id)) != str(order.id) or
                str(received.get("payment_attempt_id", attempt.id)) != str(attempt.id) or
                (yk.payment_account_id(payment) and yk.payment_account_id(payment) != YOOKASSA_SHOP_ID)):
            await cb.answer("ЮKassa не подтвердила оплату по этому заказу.", show_alert=True)
            return
    except Exception as exc:
        logger.warning("Не удалось проверить платёж №%s: %s", attempt.id, type(exc).__name__)
        await cb.answer("Не удалось проверить платёж в ЮKassa. Попробуйте позже.", show_alert=True)
        return
    await enqueue_receipt(session, attempt, order, payment, notify=False)
    await session.commit()
    await _show_receipt(cb, session, attempt.id)
    await cb.answer()


async def _show_receipt(cb: CallbackQuery, session: AsyncSession, attempt_id: int) -> None:
    receipt = await session.get(ManualReceipt, attempt_id)
    attempt = await session.get(PaymentAttempt, attempt_id)
    if not receipt or not attempt:
        await edit_or_answer(cb, "Платёж не найден.", _keyboard([
            [_button("К очереди чеков", "receipts:queue:0")]
        ]))
        return
    order = await session.get(Order, attempt.order_id)
    date = (receipt.paid_at.astimezone(MOSCOW).strftime("%d.%m.%Y в %H:%M МСК")
            if receipt.paid_at else "проверьте дату в ЮKassa")
    lines = [
        f"<b>Заказ №{order.id} · чек НПД</b>",
        f"Статус: {STATUS_LABELS[receipt.status]}",
        "",
        f"Наименование: {escape(receipt.title)}",
        f"Сумма: {attempt.amount:.2f} ₽",
        f"Дата оплаты: {date}",
        f"Покупатель: {escape(order.customer_fio or '—')}",
        f"Email: {escape(receipt.email or 'не указан')}",
        f"ID платежа: <code>{escape(attempt.payment_id or '—')}</code>",
    ]
    if receipt.status == "waiting":
        lines.extend(("", "В «Моём налоге» откройте «Новая продажа» и внесите данные выше. "
                      "Для покупки от частного лица выберите «Физическому лицу». "
                      "Если чек уже есть, скачайте его PDF."))
    if receipt.status == "queued":
        lines.extend(("", "При обычной доставке письмо с чеком отправится после методички."))
    if receipt.sent_at:
        sent = receipt.sent_at.astimezone(MOSCOW)
        lines.append(f"Письмо принято: {sent:%d.%m.%Y в %H:%M} МСК")
    if receipt.error:
        lines.extend(("", escape(receipt.error)))
    rows = []
    if receipt.status == "waiting" and receipt.email:
        rows.append([_button("Загрузить PDF чека", f"receipts:upload:{attempt_id}")])
    elif receipt.status in {"failed", "unknown"}:
        rows.append([_button("Повторить отправку", f"receipts:retry-confirm:{attempt_id}")])
    rows.append([_button("Обновить", f"receipts:open:{attempt_id}"),
                 _button("К очереди чеков", "receipts:queue:0")])
    await edit_or_answer(cb, "\n".join(lines), _keyboard(rows))


@router.callback_query(F.data.regexp(r"^receipts:open:[0-9]{1,10}$"))
async def open_receipt(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await _show_receipt(cb, session, _last_number(cb))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^receipts:upload:[0-9]{1,10}$"))
async def upload_prompt(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    attempt_id = _last_number(cb)
    receipt = await session.get(ManualReceipt, attempt_id)
    attempt = await session.get(PaymentAttempt, attempt_id)
    if not receipt or not attempt or receipt.status != "waiting" or not receipt.email:
        await cb.answer("PDF сейчас нельзя загрузить. Обновите карточку.", show_alert=True)
        return
    await state.set_state(ReceiptUpload.pdf)
    await state.set_data({"attempt_id": attempt_id})
    await edit_or_answer(cb,
        f"Пришлите PDF чека по заказу №{attempt.order_id} на сумму {attempt.amount:g} ₽.\n"
        f"После загрузки бот отправит файл на {escape(receipt.email)}.",
        _keyboard([[_button("Отменить загрузку", f"receipts:open:{attempt_id}")]]),
    )
    await cb.answer()


@router.message(ReceiptUpload.pdf, Command("cancel"))
async def cancel_upload(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Загрузка отменена.", reply_markup=_keyboard([
        [_button("К очереди чеков", "receipts:queue:0")]
    ]))


@router.message(ReceiptUpload.pdf, F.document)
async def upload_receipt(message: Message, state: FSMContext) -> None:
    attempt_id = (await state.get_data()).get("attempt_id")
    if not attempt_id:
        await state.clear()
        await message.answer("Снова откройте платёж и нажмите «Загрузить PDF чека».")
        return
    document = message.document
    if (
        not document.file_name or not document.file_name.lower().endswith(".pdf") or
        not document.file_size or document.file_size > MAX_RECEIPT_BYTES
    ):
        await message.answer("Пришлите чек PDF-файлом до 8 МБ.")
        return
    try:
        pdf = await message.bot.download(document, destination=io.BytesIO(), timeout=25)
        if pdf is None:
            await message.answer("Telegram не вернул файл. Отправьте PDF ещё раз.")
            return
    except (TelegramAPIError, OSError, TimeoutError):
        await message.answer("Не удалось скачать PDF из Telegram. Попробуйте отправить его ещё раз.")
        return
    try:
        await store_receipt_pdf(attempt_id, pdf.getvalue(), message.from_user.id)
    except ValueError as exc:
        await message.answer(escape(str(exc)))
        return
    except Exception as exc:
        logger.error("Не удалось сохранить PDF чека по попытке №%s (%s)", attempt_id, type(exc).__name__)
        await message.answer("Не удалось сохранить чек. Проверьте статус платежа в очереди.")
        return
    await state.clear()
    await message.answer(
        "Чек сохранён. Бот отправит его отдельным письмом.",
        reply_markup=_keyboard([[_button("Проверить отправку", f"receipts:open:{attempt_id}")]]),
    )


@router.message(ReceiptUpload.pdf, ~F.text.startswith("/"))
async def require_pdf(message: Message) -> None:
    await message.answer("Пришлите чек PDF-файлом или нажмите «Отменить загрузку».")


@router.callback_query(F.data.regexp(r"^receipts:retry-confirm:[0-9]{1,10}$"))
async def confirm_retry(cb: CallbackQuery) -> None:
    attempt_id = _last_number(cb)
    await edit_or_answer(cb,
        "Отправить сохранённый PDF повторно? Если результат прошлой отправки неизвестен, "
        "сначала проверьте, дошло ли письмо.",
        _keyboard([
            [_button("Отправить повторно", f"receipts:retry:{attempt_id}")],
            [_button("Назад", f"receipts:open:{attempt_id}")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.regexp(r"^receipts:retry:[0-9]{1,10}$"))
async def retry_receipt(cb: CallbackQuery, session: AsyncSession) -> None:
    attempt_id = _last_number(cb)
    try:
        await queue_receipt_retry(attempt_id)
    except ValueError as exc:
        await cb.answer(str(exc), show_alert=True)
        return
    await _show_receipt(cb, session, attempt_id)
    await cb.answer("Чек поставлен в очередь.")
