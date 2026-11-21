from aiogram.fsm.state import State, StatesGroup


class RegistrationFlow(StatesGroup):
    fio = State()
    email = State()
    consent = State()


class SupportFlow(StatesGroup):
    waiting_message = State()


class AddProduct(StatesGroup):
    title = State()
    subtitle = State()
    description = State()
    includes = State()
    price = State()
    file_format = State()
    file = State()


class EditProduct(StatesGroup):
    value = State()


class TicketReply(StatesGroup):
    waiting = State()


class ProfileEdit(StatesGroup):
    fio = State()
    email = State()


class DebugStates(StatesGroup):
    user_id = State()
    user_selected = State()
    user_edit_fio = State()
    user_edit_email = State()

    order_id = State()
    order_status = State()
    product_id = State()
    product_file = State()
    broadcast = State()
    dm_user_id = State()
    dm_text = State()