from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from db.models import Product
from handlers.admin.common import admin_router
from handlers.common import edit_or_answer
from states import AddProduct, EditProduct

router = admin_router()

CANCEL = Kb(inline_keyboard=[[Btn(text="✖️ Отмена", callback_data="adm:products")]])


# ── Список и карточка ────────────────────────────────────────────────────
def _list_kb(products: list[Product]) -> Kb:
    rows = [
        [Btn(text=f"{'✅' if p.is_active else '🚫'} {p.emoji} {p.title} — {texts.price(p.price)}",
             callback_data=f"adm:p:{p.id}")]
        for p in products
    ]
    rows.append([Btn(text="➕ Добавить товар", callback_data="adm:padd")])
    rows.append([Btn(text="⬅️ Назад", callback_data="adm:panel")])
    return Kb(inline_keyboard=rows)


def _view_text(p: Product) -> str:
    return (
        f"{texts.product_card(p)}\n\n━━━━━━━━━━━━━━━\n"
        f"Статус: {'✅ активен' if p.is_active else '🚫 скрыт'}\n"
        f"Файл: {'📎 загружен' if p.file_id else '❌ нет'}"
    )


def _view_kb(p: Product) -> Kb:
    return Kb(inline_keyboard=[
        [Btn(text="✏️ Цена", callback_data=f"adm:pe:{p.id}:price"),
         Btn(text="✏️ Описание", callback_data=f"adm:pe:{p.id}:desc")],
        [Btn(text="🚫 Скрыть" if p.is_active else "✅ Активировать", callback_data=f"adm:pt:{p.id}")],
        [Btn(text="⬅️ К списку", callback_data="adm:products")],
    ])


async def _show_list(session: AsyncSession) -> tuple[str, Kb]:
    products = list((await session.execute(select(Product).order_by(Product.id))).scalars())
    head = "📦 <b>Управление товарами</b>\n\n✅ — активен, 🚫 — скрыт"
    return (head if products else head + "\n\nТоваров пока нет."), _list_kb(products)


@router.callback_query(F.data == "adm:products")
async def cb_products(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    text, kb = await _show_list(session)
    await edit_or_answer(cb, text, kb)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:p:\d+$"))
async def cb_product_view(cb: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    p = await session.get(Product, int(cb.data.split(":")[2]))
    if not p:
        await cb.answer("Товар не найден", show_alert=True)
        return
    await edit_or_answer(cb, _view_text(p), _view_kb(p))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^adm:pt:\d+$"))
async def cb_toggle(cb: CallbackQuery, session: AsyncSession) -> None:
    p = await session.get(Product, int(cb.data.split(":")[2]))
    if not p:
        await cb.answer("Товар не найден", show_alert=True)
        return
    p.is_active = not p.is_active
    await edit_or_answer(cb, _view_text(p), _view_kb(p))
    await cb.answer("Товар активирован" if p.is_active else "Товар скрыт из каталога")


# ── Редактирование цены / описания ───────────────────────────────────────
@router.callback_query(F.data.regexp(r"^adm:pe:\d+:(price|desc)$"))
async def cb_edit(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, pid, field = cb.data.split(":")
    await state.set_state(EditProduct.value)
    await state.update_data(product_id=int(pid), field=field)
    prompt = "Введите новую <b>цену</b> в рублях (целое число):" if field == "price" \
        else "Введите новое <b>описание</b> (до 2000 символов):"
    await edit_or_answer(cb, f"✏️ {prompt}", Kb(inline_keyboard=[[Btn(text="✖️ Отмена", callback_data=f"adm:p:{pid}")]]))
    await cb.answer()


@router.message(EditProduct.value, F.text)
async def got_edit_value(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    p = await session.get(Product, data["product_id"])
    if not p:
        await state.clear()
        await message.answer("Товар не найден")
        return
    value = message.text.strip()
    if data["field"] == "price":
        if not value.isdigit() or not 0 < int(value) <= 1_000_000:
            await message.answer("⚠️ Нужно целое число от 1 до 1 000 000.")
            return
        p.price = int(value)
    else:
        if not value or len(value) > 2000:
            await message.answer("⚠️ Описание: от 1 до 2000 символов.")
            return
        p.description = value
    await state.clear()
    await message.answer("✅ Сохранено.\n\n" + _view_text(p), reply_markup=_view_kb(p))


# ── Добавление товара (FSM) ──────────────────────────────────────────────
@router.callback_query(F.data == "adm:padd")
async def add_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddProduct.title)
    await edit_or_answer(cb, "➕ <b>Новый товар</b>\nШаг 1/7 — введите <b>название</b>:", CANCEL)
    await cb.answer()


@router.message(AddProduct.title, F.text)
async def add_title(message: Message, state: FSMContext) -> None:
    if not 0 < len(message.text.strip()) <= 255:
        await message.answer("⚠️ Название: от 1 до 255 символов.", reply_markup=CANCEL)
        return
    await state.update_data(title=message.text.strip())
    await state.set_state(AddProduct.subtitle)
    await message.answer("Шаг 2/7 — введите <b>подзаголовок</b> (или «-», чтобы пропустить):", reply_markup=CANCEL)


@router.message(AddProduct.subtitle, F.text)
async def add_subtitle(message: Message, state: FSMContext) -> None:
    v = message.text.strip()
    if len(v) > 255:
        await message.answer("⚠️ Не длиннее 255 символов.", reply_markup=CANCEL)
        return
    await state.update_data(subtitle="" if v == "-" else v)
    await state.set_state(AddProduct.description)
    await message.answer("Шаг 3/7 — введите <b>описание</b> (до 2000 символов):", reply_markup=CANCEL)


@router.message(AddProduct.description, F.text)
async def add_description(message: Message, state: FSMContext) -> None:
    v = message.text.strip()
    if not v or len(v) > 2000:
        await message.answer("⚠️ Описание: от 1 до 2000 символов.", reply_markup=CANCEL)
        return
    await state.update_data(description=v)
    await state.set_state(AddProduct.includes)
    await message.answer(
        "Шаг 4/7 — что входит? Каждый пункт с новой строки (до 10 пунктов):", reply_markup=CANCEL
    )


@router.message(AddProduct.includes, F.text)
async def add_includes(message: Message, state: FSMContext) -> None:
    items = [ln.strip(" -•✔✓") for ln in message.text.splitlines() if ln.strip(" -•✔✓")]
    if not 1 <= len(items) <= 10 or any(len(i) > 120 for i in items):
        await message.answer("⚠️ Нужно от 1 до 10 пунктов, каждый до 120 символов.", reply_markup=CANCEL)
        return
    await state.update_data(includes=items)
    await state.set_state(AddProduct.price)
    await message.answer("Шаг 5/7 — введите <b>цену</b> в рублях (целое число):", reply_markup=CANCEL)


@router.message(AddProduct.price, F.text)
async def add_price(message: Message, state: FSMContext) -> None:
    v = message.text.strip()
    if not v.isdigit() or not 0 < int(v) <= 1_000_000:
        await message.answer("⚠️ Нужно целое число от 1 до 1 000 000.", reply_markup=CANCEL)
        return
    await state.update_data(price=int(v))
    await state.set_state(AddProduct.file_format)
    await message.answer("Шаг 6/7 — введите <b>формат</b> файла (например, PDF):", reply_markup=CANCEL)


@router.message(AddProduct.file_format, F.text)
async def add_format(message: Message, state: FSMContext) -> None:
    v = message.text.strip()
    if not 0 < len(v) <= 20:
        await message.answer("⚠️ Формат: до 20 символов.", reply_markup=CANCEL)
        return
    await state.update_data(file_format=v.upper())
    await state.set_state(AddProduct.file)
    await message.answer("Шаг 7/7 — отправьте <b>PDF-файл</b> методички (как документ):", reply_markup=CANCEL)


@router.message(AddProduct.file, F.document)
async def add_file(message: Message, state: FSMContext, session: AsyncSession) -> None:
    doc = message.document
    is_pdf = doc.mime_type == "application/pdf" or (doc.file_name or "").lower().endswith(".pdf")
    if not is_pdf:
        await message.answer("⚠️ Нужен файл в формате PDF.", reply_markup=CANCEL)
        return
    data = await state.get_data()
    p = Product(
        title=data["title"], subtitle=data["subtitle"], description=data["description"],
        includes=data["includes"], price=data["price"], file_format=data["file_format"],
        file_id=doc.file_id, is_active=True,
    )
    session.add(p)
    await session.flush()
    await state.clear()
    await message.answer("✅ Товар добавлен.\n\n" + _view_text(p), reply_markup=_view_kb(p))


# Любой неподходящий ввод на шагах добавления/редактирования
@router.message(AddProduct.title)
@router.message(AddProduct.subtitle)
@router.message(AddProduct.description)
@router.message(AddProduct.includes)
@router.message(AddProduct.price)
@router.message(AddProduct.file_format)
@router.message(EditProduct.value)
async def need_text(message: Message) -> None:
    await message.answer("✏️ Отправьте ответ текстом.", reply_markup=CANCEL)


@router.message(AddProduct.file)
async def need_file(message: Message) -> None:
    await message.answer("📎 Отправьте PDF как документ (скрепка → файл).", reply_markup=CANCEL)