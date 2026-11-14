from aiogram.fsm.state import State, StatesGroup


class AddRecord(StatesGroup):
    waiting_title = State()
    waiting_description = State()
    waiting_fields = State()
    confirm = State()
