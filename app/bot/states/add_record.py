from aiogram.fsm.state import State, StatesGroup


class AddRecord(StatesGroup):
    """Полный сценарий добавления записи."""

    waiting_type = State()
    waiting_title = State()
    waiting_description = State()
    waiting_fields = State()
    waiting_field_name = State()
    waiting_field_type = State()
    waiting_field_value = State()
    waiting_tags = State()
    waiting_attachments = State()
    waiting_visibility = State()
    waiting_groups = State()
    preview = State()
    confirm = State()


class EditRecord(StatesGroup):
    """Редактирование названия, описания и отдельных полей."""

    waiting_title = State()
    waiting_description = State()
    waiting_field_name = State()
    waiting_field_type = State()
    waiting_field_value = State()
    waiting_tags = State()
