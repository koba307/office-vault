import hashlib
import html
from io import BytesIO
from uuid import UUID, uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot.handlers.menu import SORT_DATE, TYPE_TITLES, render_records_list
from app.bot.keyboards.pagination import confirm_keyboard
from app.bot.states.add_record import AddRecord, EditRecord
from app.core.config import settings
from app.core.encryption import decrypt_secret, encrypt_secret
from app.database.models import (
    AccessMode,
    FieldType,
    File,
    Group,
    RecordType,
    RoleCode,
)
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active, log_audit
from app.services.categories import get_category, get_category_by_id, list_categories
from app.services.favorites.service import is_favorite
from app.services.permissions import PermissionService
from app.services.recent.service import add_recent
from app.services.records.fields import (
    FIELD_TYPE_HINTS,
    FIELD_TYPE_LABELS,
    SECRET_MASK,
    FieldValidationError,
    default_field_definitions,
    format_field_value,
    format_raw_field_value,
    is_secret_field_type,
    normalize_field_type,
    parse_multiselect,
    validate_field_value,
)
from app.services.records.service import (
    PERMANENTLY_DELETABLE_STATUSES,
    RecordError,
    add_attachment,
    archive_record,
    create_record,
    get_record,
    list_attachments,
    move_record,
    permanent_delete,
    remove_attachment,
    restore_record,
    set_record_field,
    soft_delete_record,
    update_record,
    update_record_tags,
    validate_record_description,
    validate_record_title,
)
from app.services.records.tags import (
    TagError,
    add_tag_to_record,
    create_tag,
    get_record_tags,
    get_tag_by_key,
    list_tags,
    remove_tag_from_record,
    tag_key,
)
from app.services.records.versions import (
    RecordVersionError,
    get_version,
    get_version_diff,
    list_versions,
    restore_version,
)

router = Router()

MANAGER_ROLES = (RoleCode.SUPERADMIN, RoleCode.BOSS)

VISIBILITY_TITLES: dict[AccessMode | None, str] = {
    None: "Как в разделе",
    AccessMode.ALL_EMPLOYEES: "Все сотрудники",
    AccessMode.BOSSES_ONLY: "Руководители",
    AccessMode.GROUP: "Выбранные группы",
    AccessMode.PRIVATE: "Только я",
}

_VISIBILITY_ORDER: tuple[AccessMode | None, ...] = (
    None,
    AccessMode.ALL_EMPLOYEES,
    AccessMode.BOSSES_ONLY,
    AccessMode.GROUP,
    AccessMode.PRIVATE,
)


# ---------------------------------------------------------------------------
# Общие помощники
# ---------------------------------------------------------------------------


def _is_manager(user) -> bool:
    return user is not None and user.role in MANAGER_ROLES


def _parse_uuid(value: str | None) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _escape(value) -> str:
    return html.escape(str(value), quote=False)


def _clip(value, limit: int = 120) -> str:
    text = str(value)
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


async def _send(
    target: Message | CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Редактирует сообщение с кнопкой или отправляет новое."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
            return
        except TelegramBadRequest:
            pass
        await target.message.answer(text, reply_markup=markup, parse_mode="HTML")
        return
    await target.answer(text, reply_markup=markup, parse_mode="HTML")


def _button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def _cancel_row(
    cancel: str = "record:add:cancel", back: str | None = None
) -> list[InlineKeyboardButton]:
    row = [_button("✖️ Отменить", cancel)]
    if back:
        row.insert(0, _button("⬅️ Назад", back))
    return row


def _field_entry(label: str, field_type: str, value, *, encrypted: bool = False) -> dict:
    return {
        "__field__": True,
        "label": label,
        "field_type": field_type,
        "value": value,
        "encrypted": encrypted,
    }


def _entry_display(entry: dict) -> str:
    if entry.get("encrypted"):
        return SECRET_MASK
    field_type = normalize_field_type(entry.get("field_type", FieldType.TEXT.value))
    return format_raw_field_value(field_type, entry.get("value"))


def _fields_lines(fields: dict[str, dict]) -> list[str]:
    if not fields:
        return ["—"]
    lines = []
    for entry in fields.values():
        label = _escape(entry.get("label", "Поле"))
        lines.append(f"• {label}: {_escape(_clip(_entry_display(entry)))}")
    return lines


def _split_tags(raw: str) -> list[str]:
    return parse_multiselect(raw or "")


def _type_title(record_type: RecordType | str) -> str:
    try:
        return TYPE_TITLES[RecordType(record_type)]
    except (ValueError, KeyError):
        return str(record_type)


def _type_keyboard(prefix: str, back: str) -> InlineKeyboardMarkup:
    buttons = [
        _button(FIELD_TYPE_LABELS[field_type], f"{prefix}:{field_type.value}")
        for field_type in FieldType
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    cancel = "record:add:cancel" if prefix == "record:ftype" else "record:edit:cancel"
    rows.append(_cancel_row(cancel, back))
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _load_user(session, telegram_id: int):
    return await get_user_by_telegram_id(session, telegram_id)


async def _has_permission(session, user, action: str, target: str, target_id) -> bool:
    return await PermissionService.can(user, action, target, target_id, session)


# ---------------------------------------------------------------------------
# Старт добавления записи
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("record:add:start:"))
async def add_choose_type(callback: CallbackQuery, state: FSMContext) -> None:
    category_code = (callback.data or "").removeprefix("record:add:start:")
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        category = await get_category(session, category_code)
        if category is None or category.status != "ACTIVE":
            await callback.answer("Раздел не найден", show_alert=True)
            return
        if not await _has_permission(session, user, "CREATE", "category", category.id):
            await callback.answer("Нет прав на создание записей", show_alert=True)
            return

    await state.clear()
    await state.update_data(category=category.code)
    await state.set_state(AddRecord.waiting_type)
    rows = [
        [_button(title, f"record:add:{record_type.value}:{category.code}")]
        for record_type, title in TYPE_TITLES.items()
    ]
    rows.append([_button("⬅️ Назад", "menu:main")])
    await _send(
        callback,
        f"➕ <b>{_escape(category.name)}</b>\n\nВыберите тип информации:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(
    F.data.regexp(r"^record:add:[A-Z][A-Z_]*:[a-z0-9][a-z0-9_-]{0,31}$")
)
async def start_add_record(callback: CallbackQuery, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    rec_type, category_code = parts[2], parts[3]
    try:
        record_type = RecordType(rec_type)
    except ValueError:
        await callback.answer("Неизвестный тип записи", show_alert=True)
        return

    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        category = await get_category(session, category_code)
        if category is None or category.status != "ACTIVE":
            await callback.answer("Раздел не найден", show_alert=True)
            return
        if not await _has_permission(session, user, "CREATE", "category", category.id):
            await callback.answer("Нет прав на создание записей", show_alert=True)
            return

    await state.clear()
    await state.update_data(
        record_type=record_type.value,
        category=category.code,
        title=None,
        description=None,
        fields={},
        tags=[],
        attachments=[],
        visibility=None,
        group_codes=[],
        custom_counter=0,
    )
    await state.set_state(AddRecord.waiting_title)
    await _send(
        callback,
        "📝 <b>Новая запись</b>\n\n"
        f"Раздел: {_escape(category.name)}\n"
        f"Тип: {_type_title(record_type)}\n\n"
        "Введите название записи:",
        InlineKeyboardMarkup(
            inline_keyboard=[
                _cancel_row(back=f"menu:list:{category.code}:{record_type.value}:{SORT_DATE}:0")
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "record:add:cancel")
async def add_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _send(
        callback,
        "Добавление записи отменено.",
        InlineKeyboardMarkup(
            inline_keyboard=[[_button("🏠 В главное меню", "menu:main")]]
        ),
    )
    await callback.answer("Отменено")


@router.message(AddRecord.waiting_type)
async def add_type_hint(message: Message, state: FSMContext) -> None:
    await message.answer("Выберите тип записи кнопкой выше или отмените действие.")


@router.message(AddRecord.waiting_title)
async def add_title(message: Message, state: FSMContext) -> None:
    try:
        title = validate_record_title(message.text or "")
    except RecordError as exc:
        await message.answer(f"❌ {_escape(exc)}\nВведите название ещё раз.")
        return
    await state.update_data(title=title)
    await state.set_state(AddRecord.waiting_description)
    await message.answer(
        "Введите описание записи (или нажмите «Пропустить»):",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button("⏭ Пропустить", "record:add:skip:description"),
                    _button("⬅️ Назад", "record:add:back:title"),
                ],
                _cancel_row(),
            ]
        ),
    )


@router.callback_query(F.data == "record:add:back:title")
async def add_back_title(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddRecord.waiting_title)
    await _send(
        callback,
        "Введите название записи:",
        InlineKeyboardMarkup(
            inline_keyboard=[_cancel_row(back="record:add:cancel")]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "record:add:skip:description")
async def add_skip_description(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(description=None)
    await _begin_fields(callback, state)
    await callback.answer()


@router.callback_query(F.data == "record:add:back:description")
async def add_back_description(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddRecord.waiting_description)
    await _send(
        callback,
        "Введите описание записи (или нажмите «Пропустить»):",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button("⏭ Пропустить", "record:add:skip:description"),
                    _button("⬅️ Назад", "record:add:back:title"),
                ],
                _cancel_row(),
            ]
        ),
    )
    await callback.answer()


@router.message(AddRecord.waiting_description)
async def add_description(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        description = None if raw in ("", "-") else validate_record_description(raw)
    except RecordError as exc:
        await message.answer(f"❌ {_escape(exc)}\nВведите описание ещё раз.")
        return
    await state.update_data(description=description)
    await _begin_fields(message, state)


# ---------------------------------------------------------------------------
# Поля: шаблон типа записи
# ---------------------------------------------------------------------------


async def _begin_fields(target: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    record_type = RecordType(data.get("record_type", RecordType.OTHER.value))
    definitions = [
        {
            "key": item.key,
            "label": item.label,
            "field_type": item.field_type.value,
            "required": item.required,
            "hint": item.hint or FIELD_TYPE_HINTS[item.field_type],
        }
        for item in default_field_definitions(record_type)
    ]
    await state.update_data(field_defs=definitions, field_index=0, pending_field=None)
    if not definitions:
        await _show_field_menu(target, state)
        return
    await _ask_field(target, state)


async def _ask_field(target: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    definitions = data.get("field_defs") or []
    index = int(data.get("field_index", 0))
    if index >= len(definitions):
        await _show_field_menu(target, state)
        return

    definition = definitions[index]
    field_type = normalize_field_type(definition["field_type"])
    fields = data.get("fields") or {}
    current = fields.get(definition["key"])
    await state.update_data(pending_field=definition)
    await state.set_state(AddRecord.waiting_field_value)

    marker = "обязательное" if definition.get("required") else "необязательное"
    lines = [
        f"Поле {index + 1}/{len(definitions)}: <b>{_escape(definition['label'])}</b>",
        f"Тип: {FIELD_TYPE_LABELS[field_type]} · {marker}",
        "",
        _escape(definition.get("hint") or FIELD_TYPE_HINTS[field_type]),
    ]
    if current is not None:
        lines += ["", f"Текущее значение: <code>{_escape(_clip(_entry_display(current)))}</code>"]
    back = "record:add:back_field" if index > 0 else "record:add:back:description"
    rows = [
        [
            _button("⏭ Пропустить", "record:add:skip_field"),
            _button("⬅️ Назад", back),
        ],
        [_button("👁 Предпросмотр", "record:add:preview"), _button("✖️ Отменить", "record:add:cancel")],
    ]
    await _send(target, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "record:add:skip_field")
async def add_skip_field(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    pending = data.get("pending_field") or {}
    if pending.get("required"):
        await callback.answer("Это поле обязательное", show_alert=True)
        return
    await state.update_data(field_index=int(data.get("field_index", 0)) + 1, pending_field=None)
    await _ask_field(callback, state)
    await callback.answer()


@router.callback_query(F.data == "record:add:back_field")
async def add_back_field(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    index = max(int(data.get("field_index", 0)) - 1, 0)
    await state.update_data(field_index=index, pending_field=None)
    await _ask_field(callback, state)
    await callback.answer()


@router.message(AddRecord.waiting_field_value)
async def add_field_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    pending = data.get("pending_field")
    if not pending:
        await message.answer("Поле не выбрано. Вернитесь к списку полей.")
        return

    field_type = normalize_field_type(pending.get("field_type"))
    raw = message.text or ""
    if not raw.strip() and not pending.get("required"):
        await message.answer("Введите значение или нажмите «Пропустить».")
        return
    try:
        value = validate_field_value(field_type, raw)
    except FieldValidationError as exc:
        await message.answer(f"❌ {_escape(exc)}\n\n{FIELD_TYPE_HINTS[field_type]}")
        return
    if value is None:
        if pending.get("required"):
            await message.answer("❌ Обязательное поле не может быть пустым.")
            return
        await message.answer("Введите значение или нажмите «Пропустить».")
        return

    secret = is_secret_field_type(field_type)
    entry = _field_entry(
        pending["label"],
        field_type.value,
        encrypt_secret(value) if secret else value,
        encrypted=secret,
    )
    fields = dict(data.get("fields") or {})
    fields[pending["key"]] = entry
    await state.update_data(
        fields=fields,
        field_index=int(data.get("field_index", 0)) + 1,
        pending_field=None,
    )
    await _ask_field(message, state)


@router.callback_query(F.data == "record:add:field_new")
async def add_custom_field(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(pending_field=None)
    await state.set_state(AddRecord.waiting_field_name)
    await _send(
        callback,
        "Введите название нового поля:",
        InlineKeyboardMarkup(
            inline_keyboard=[_cancel_row(back="record:add:back:fields")]
        ),
    )
    await callback.answer()


@router.message(AddRecord.waiting_field_name)
async def add_custom_field_name(message: Message, state: FSMContext) -> None:
    label = (message.text or "").strip()
    if not label:
        await message.answer("Название поля не может быть пустым. Введите ещё раз.")
        return
    if len(label) > 128:
        await message.answer("Название поля не может быть длиннее 128 символов.")
        return
    pending = {
        "key": f"field_{uuid4().hex[:8]}",
        "label": label,
        "field_type": None,
        "required": False,
        "hint": None,
    }
    await state.update_data(pending_field=pending)
    await state.set_state(AddRecord.waiting_field_type)
    await message.answer(
        f"Поле «{_escape(label)}». Выберите тип:",
        reply_markup=_type_keyboard("record:ftype", "record:add:back:fields"),
    )


@router.callback_query(F.data.startswith("record:ftype:"))
async def add_custom_field_type(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    pending = data.get("pending_field")
    if not pending:
        await callback.answer("Поле не выбрано", show_alert=True)
        return
    try:
        field_type = normalize_field_type((callback.data or "").split(":", 2)[-1])
    except FieldValidationError:
        await callback.answer("Неизвестный тип поля", show_alert=True)
        return
    pending.update(
        {
            "field_type": field_type.value,
            "hint": FIELD_TYPE_HINTS[field_type],
        }
    )
    await state.update_data(pending_field=pending)
    await state.set_state(AddRecord.waiting_field_value)
    await _send(
        callback,
        f"Поле «{_escape(pending['label'])}»\n"
        f"Тип: {FIELD_TYPE_LABELS[field_type]}\n\n"
        f"{_escape(FIELD_TYPE_HINTS[field_type])}",
        InlineKeyboardMarkup(
            inline_keyboard=[_cancel_row(back="record:add:back:fields")]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "record:add:back:fields")
async def add_back_fields(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(pending_field=None)
    await _show_field_menu(callback, state)
    await callback.answer()


async def _show_field_menu(target: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    fields = data.get("fields") or {}
    await state.update_data(pending_field=None)
    await state.set_state(AddRecord.waiting_fields)
    lines = ["🧩 <b>Поля записи</b>", "", *_fields_lines(fields), ""]
    lines.append("Добавьте произвольное поле или продолжите сценарий.")
    rows = [
        [
            _button("➕ Добавить поле", "record:add:field_new"),
            _button("👁 Предпросмотр", "record:add:preview"),
        ],
        [_button("➡️ Продолжить", "record:add:fields_done")],
        _cancel_row(back="record:add:back:description"),
    ]
    await _send(target, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "record:add:fields_done")
async def add_fields_done(callback: CallbackQuery, state: FSMContext) -> None:
    await _ask_tags(callback, state)
    await callback.answer()


@router.message(AddRecord.waiting_fields)
async def add_fields_hint(message: Message, state: FSMContext) -> None:
    await message.answer("Используйте кнопки «Добавить поле» или «Продолжить».")


# ---------------------------------------------------------------------------
# Теги, вложения, видимость и группы
# ---------------------------------------------------------------------------


async def _ask_tags(target: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    current = data.get("tags") or []
    await state.set_state(AddRecord.waiting_tags)
    lines = ["🏷 <b>Теги</b>", "", "Введите теги через запятую или нажмите «Пропустить»."]
    if current:
        lines += ["", "Уже добавлены: " + _escape(", ".join(current))]
    rows = [
        [
            _button("⏭ Пропустить", "record:add:skip:tags"),
            _button("⬅️ Назад", "record:add:back:fields"),
        ],
        _cancel_row(),
    ]
    await _send(target, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(AddRecord.waiting_tags)
async def add_tags(message: Message, state: FSMContext) -> None:
    try:
        tags = _split_tags(message.text or "")
    except FieldValidationError as exc:
        await message.answer(f"❌ {_escape(exc)}\nВведите теги ещё раз.")
        return
    await state.update_data(tags=tags)
    await _ask_attachments(message, state)


@router.callback_query(F.data == "record:add:skip:tags")
async def add_skip_tags(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(tags=[])
    await _ask_attachments(callback, state)
    await callback.answer()


async def _ask_attachments(target: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    attachments = data.get("attachments") or []
    await state.set_state(AddRecord.waiting_attachments)
    lines = ["📎 <b>Вложения</b>", ""]
    if attachments:
        for idx, item in enumerate(attachments, start=1):
            lines.append(f"{idx}. {_escape(item.get('filename') or 'файл')}")
    else:
        lines.append("Вложений пока нет.")
    lines += ["", "Отправьте файл документом или нажмите «Продолжить»."]
    rows = [
        [
            _button("📎 Добавить файл", "record:add:attach_hint"),
            _button("➡️ Продолжить", "record:add:attachments_done"),
        ],
        [
            _button("⏭ Пропустить", "record:add:skip:attachments"),
            _button("👁 Предпросмотр", "record:add:preview"),
        ],
        _cancel_row(back="record:add:back:fields"),
    ]
    await _send(target, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "record:add:attach_hint")
async def add_attach_hint(callback: CallbackQuery) -> None:
    await callback.answer("Отправьте файл документом в этот чат", show_alert=True)


@router.message(AddRecord.waiting_attachments, F.document)
async def add_attachment_document(message: Message, state: FSMContext) -> None:
    document = message.document
    if document is None:
        return
    await _append_attachment(
        message,
        state,
        {
            "file_id": document.file_id,
            "filename": document.file_name or f"document_{document.file_id[:8]}",
            "size": document.file_size,
            "mime_type": document.mime_type,
        },
    )


@router.message(AddRecord.waiting_attachments, F.photo)
async def add_attachment_photo(message: Message, state: FSMContext) -> None:
    if not message.photo:
        return
    photo = message.photo[-1]
    await _append_attachment(
        message,
        state,
        {
            "file_id": photo.file_id,
            "filename": f"photo_{photo.file_unique_id}.jpg",
            "size": photo.file_size,
            "mime_type": "image/jpeg",
        },
    )


async def _append_attachment(message: Message, state: FSMContext, item: dict) -> None:
    data = await state.get_data()
    attachments = list(data.get("attachments") or [])
    attachments.append(item)
    await state.update_data(attachments=attachments)
    await message.answer(
        f"📎 Файл «{_escape(item['filename'])}» добавлен. "
        f"Всего вложений: {len(attachments)}.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button("➡️ Продолжить", "record:add:attachments_done"),
                    _button("👁 Предпросмотр", "record:add:preview"),
                ],
                _cancel_row(back="record:add:back:fields"),
            ]
        ),
    )


@router.message(AddRecord.waiting_attachments)
async def add_attachments_hint(message: Message, state: FSMContext) -> None:
    await message.answer("Отправьте файл документом или используйте кнопки под сообщением.")


@router.callback_query(F.data == "record:add:attachments_done")
async def add_attachments_done(callback: CallbackQuery, state: FSMContext) -> None:
    await _ask_visibility(callback, state)
    await callback.answer()


@router.callback_query(F.data == "record:add:skip:attachments")
async def add_skip_attachments(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(attachments=[])
    await _ask_visibility(callback, state)
    await callback.answer()


async def _ask_visibility(target: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    current = data.get("visibility")
    await state.set_state(AddRecord.waiting_visibility)
    rows = [
        [
            _button(
                ("✅ " if (mode.value if mode else None) == current else "")
                + title,
                f"record:add:visibility:{mode.value if mode else 'INHERIT'}",
            )
        ]
        for mode, title in ((mode, VISIBILITY_TITLES[mode]) for mode in _VISIBILITY_ORDER)
    ]
    rows.append(
        [
            _button("⏭ Пропустить", "record:add:visibility:INHERIT"),
            _button("⬅️ Назад", "record:add:back:fields"),
        ]
    )
    rows.append(_cancel_row())
    await _send(
        target,
        "👁 <b>Видимость записи</b>\n\nВыберите режим доступа:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("record:add:visibility:"))
async def add_visibility(callback: CallbackQuery, state: FSMContext) -> None:
    raw = (callback.data or "").split(":", 3)[-1]
    if raw == "INHERIT":
        mode = None
    else:
        try:
            mode = AccessMode(raw)
        except ValueError:
            await callback.answer("Неизвестный режим доступа", show_alert=True)
            return
    await state.update_data(visibility=mode.value if mode else None)
    if mode is AccessMode.GROUP:
        await _ask_groups(callback, state)
    else:
        await state.update_data(group_codes=[])
        await _show_preview(callback, state)
    await callback.answer()


async def _ask_groups(target: Message | CallbackQuery, state: FSMContext) -> None:
    async with async_session() as session:
        result = await session.execute(select(Group).order_by(Group.name))
        groups = [
            {"id": str(group.id), "code": group.code, "name": group.name}
            for group in result.scalars().all()
        ]
    if not groups:
        await state.update_data(group_codes=[])
        await _show_preview(target, state)
        return

    data = await state.get_data()
    selected = set(data.get("group_codes") or [])
    await state.update_data(available_groups=groups)
    await state.set_state(AddRecord.waiting_groups)
    rows = []
    for group in groups:
        mark = "✅" if group["code"] in selected else "⬜"
        rows.append(
            [_button(f"{mark} {group['name']}", f"record:add:group:{group['id']}")]
        )
    rows.append([_button("✅ Готово", "record:add:groups_done")])
    rows.append(
        [
            _button("⏭ Пропустить", "record:add:skip:groups"),
            _button("⬅️ Назад", "record:add:back:visibility"),
        ]
    )
    rows.append(_cancel_row())
    await _send(
        target,
        "🏢 <b>Доступ по группам</b>\n\nОтметьте группы, которым видна запись:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("record:add:group:"))
async def add_group_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    group_id = (callback.data or "").split(":", 3)[-1]
    data = await state.get_data()
    groups = data.get("available_groups") or []
    group = next((item for item in groups if item["id"] == group_id), None)
    if group is None:
        await callback.answer("Группа не найдена", show_alert=True)
        return
    selected = list(data.get("group_codes") or [])
    if group["code"] in selected:
        selected.remove(group["code"])
    else:
        selected.append(group["code"])
    await state.update_data(group_codes=selected)

    rows = []
    for item in groups:
        mark = "✅" if item["code"] in selected else "⬜"
        rows.append([_button(f"{mark} {item['name']}", f"record:add:group:{item['id']}")])
    rows.append([_button("✅ Готово", "record:add:groups_done")])
    rows.append(
        [
            _button("⏭ Пропустить", "record:add:skip:groups"),
            _button("⬅️ Назад", "record:add:back:visibility"),
        ]
    )
    rows.append(_cancel_row())
    await _send(
        callback,
        "🏢 <b>Доступ по группам</b>\n\nОтметьте группы, которым видна запись:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data == "record:add:groups_done")
async def add_groups_done(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_preview(callback, state)
    await callback.answer()


@router.callback_query(F.data == "record:add:skip:groups")
async def add_skip_groups(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(group_codes=[])
    await _show_preview(callback, state)
    await callback.answer()


@router.callback_query(F.data == "record:add:back:visibility")
async def add_back_visibility(callback: CallbackQuery, state: FSMContext) -> None:
    await _ask_visibility(callback, state)
    await callback.answer()


@router.message(AddRecord.waiting_visibility)
async def add_visibility_hint(message: Message, state: FSMContext) -> None:
    await message.answer("Выберите режим доступа кнопкой под сообщением.")


@router.message(AddRecord.waiting_groups)
async def add_groups_hint(message: Message, state: FSMContext) -> None:
    await message.answer("Отметьте группы кнопками или нажмите «Пропустить».")


# ---------------------------------------------------------------------------
# Предпросмотр и сохранение
# ---------------------------------------------------------------------------


def _preview_lines(data: dict) -> list[str]:
    fields = data.get("fields") or {}
    tags = data.get("tags") or []
    attachments = data.get("attachments") or []
    visibility = data.get("visibility")
    mode = None
    if visibility:
        try:
            mode = AccessMode(visibility)
        except ValueError:
            mode = None
    lines = [
        "👁 <b>Предпросмотр записи</b>",
        "",
        f"Раздел: {_escape(data.get('category', '—'))}",
        f"Тип: {_type_title(data.get('record_type', ''))}",
        f"Название: <b>{_escape(data.get('title') or '—')}</b>",
        f"Описание: {_escape(_clip(data.get('description') or '—'))}",
        "",
        "<b>Поля:</b>",
        *_fields_lines(fields),
        "",
        f"Теги: {_escape(', '.join(tags)) if tags else '—'}",
        f"Вложения: {len(attachments)}",
        f"Видимость: {_escape(VISIBILITY_TITLES.get(mode, 'Как в разделе'))}",
    ]
    if mode is AccessMode.GROUP:
        lines.append(f"Группы: {len(data.get('group_codes') or [])}")
    return lines


async def _show_preview(target: Message | CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(AddRecord.preview)
    rows = [
        [_button("✅ Сохранить", "record:add:save")],
        [
            _button("⬅️ Назад", "record:add:back:fields"),
            _button("✖️ Отменить", "record:add:cancel"),
        ],
    ]
    await _send(target, "\n".join(_preview_lines(data)), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "record:add:preview")
async def add_preview(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_preview(callback, state)
    await callback.answer()


@router.callback_query(F.data == "record:add:back:preview")
async def add_back_preview(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_preview(callback, state)
    await callback.answer()


@router.callback_query(F.data == "record:add:save")
async def add_save(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddRecord.confirm)
    await _send(
        callback,
        "Сохранить запись?",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [_button("✅ Сохранить", "record:add:confirm")],
                [
                    _button("⬅️ Назад", "record:add:back:preview"),
                    _button("✖️ Отменить", "record:add:cancel"),
                ],
            ]
        ),
    )
    await callback.answer()


@router.message(AddRecord.preview)
async def add_preview_hint(message: Message, state: FSMContext) -> None:
    await message.answer("Используйте кнопки «Сохранить», «Назад» или «Отменить».")


@router.message(AddRecord.confirm)
async def add_confirm_hint(message: Message, state: FSMContext) -> None:
    await message.answer("Нажмите «Сохранить» ещё раз для подтверждения.")


@router.callback_query(F.data == "record:add:confirm")
async def add_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        record_type = RecordType(data.get("record_type", RecordType.OTHER.value))
    except ValueError:
        await state.clear()
        await callback.answer("Тип записи повреждён. Начните заново.", show_alert=True)
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        category = await get_category(session, data.get("category", ""))
        if category is None:
            await callback.answer("Раздел не найден", show_alert=True)
            await state.clear()
            return
        if not await _has_permission(session, user, "CREATE", "category", category.id):
            await callback.answer("Нет прав на создание записей", show_alert=True)
            return
        try:
            record = await create_record(
                session,
                category_code=category.code,
                record_type=record_type,
                title=data.get("title") or "",
                description=data.get("description"),
                created_by=user,
                fields=data.get("fields") or {},
                tags=data.get("tags") or [],
                attachments=data.get("attachments") or [],
                access_mode=data.get("visibility"),
                group_codes=data.get("group_codes") or [],
            )
        except (RecordError, FieldValidationError) as exc:
            await callback.answer("Проверьте данные записи", show_alert=True)
            await state.set_state(AddRecord.preview)
            await callback.message.answer(
                f"❌ {_escape(exc)}",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [_button("👁 К предпросмотру", "record:add:back:preview")],
                        [_button("✖️ Отменить", "record:add:cancel")],
                    ]
                ),
                parse_mode="HTML",
            )
            return
        await log_audit(session, user.id, "CREATE_RECORD", "Record", str(record.id))

    await state.clear()
    await callback.message.answer(
        f"✅ Запись «{_escape(record.title)}» создана.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [_button("📄 Открыть запись", f"record:view:{record.id}")],
                [_button("🏠 В главное меню", "menu:main")],
            ]
        ),
        parse_mode="HTML",
    )
    await callback.answer("Запись создана")


# ---------------------------------------------------------------------------
# Карточка записи и секреты
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("type:"))
async def type_callback(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Некорректный запрос")
        return

    rec_type = parts[1].upper()
    category = parts[2]

    try:
        RecordType(rec_type)
    except ValueError:
        await callback.answer("Неизвестный тип")
        return

    await render_records_list(callback, category, rec_type, SORT_DATE, 0)


@router.callback_query(F.data.startswith("record:view:"))
async def view_record(callback: CallbackQuery) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    await render_record_card(callback, rid)


def _favorite_button(record_id: UUID, is_fav: bool) -> InlineKeyboardButton:
    if is_fav:
        return _button("💔 Убрать из избранного", f"favorite:remove:{record_id}")
    return _button("⭐ В избранное", f"favorite:add:{record_id}")


async def render_record_card(
    callback: CallbackQuery, rid: UUID, notice: str | None = None
) -> None:
    """Карточка записи. notice — текст уведомления после успешного действия в карточке."""
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "VIEW", "record", rid):
            await callback.answer("Нет доступа к записи", show_alert=True)
            return
        record = await get_record(session, rid)
        if not record:
            await callback.answer("Запись не найдена")
            return
        # Просмотр фиксируется в истории: только id записи и дата,
        # значения полей и секреты туда не попадают
        await add_recent(session, user.id, rid)
        can_edit = await _has_permission(session, user, "EDIT", "record", rid)
        can_archive = await _has_permission(session, user, "ARCHIVE", "record", rid)
        can_delete = await _has_permission(session, user, "DELETE", "record", rid)
        is_fav = await is_favorite(session, user.id, rid)

    lines = [f"📄 <b>{_escape(record.title)}</b>", f"Тип: {_type_title(record.record_type)}"]
    if record.description:
        lines += ["", _escape(record.description)]
    lines.append("")
    buttons: list[list[InlineKeyboardButton]] = []
    for index, field in enumerate(sorted(record.fields, key=lambda item: (item.sort_order, item.field_key))):
        display = _escape(_clip(format_field_value(
            field.field_type,
            value_text=field.value_text,
            value_json=field.value_json,
            encrypted_value=field.encrypted_value,
        )))
        lines.append(f"{_escape(field.field_label)}: {display}")
        if field.encrypted_value:
            buttons.append(
                [_button(f"👁 Показать {_clip(field.field_label, 30)}", f"rr:{record.id}:{index}")]
            )
    if record.tags:
        tags = ", ".join(link.tag.name for link in record.tags if link.tag)
        lines += ["", f"🏷 {_escape(tags)}"]
    buttons.append([_favorite_button(record.id, is_fav)])
    if record.record_type == RecordType.FILES:
        buttons.append([_button("⬇️ Скачать файл", f"record:download:{record.id}")])
    if can_edit:
        buttons.append([_button("✏️ Редактировать", f"record:edit:{record.id}")])
        buttons.append([_button("🏷 Теги", f"record:tags:{record.id}")])
    buttons.append([_button("📝 История", f"record:history:{record.id}")])
    buttons.extend(
        lifecycle_buttons(record, can_edit=can_edit, can_archive=can_archive, can_delete=can_delete)
    )
    buttons.append([_button("⬅️ Назад", "menu:main")])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer(notice)


def _resolve_field_by_index(record, index: int):
    fields = sorted(record.fields, key=lambda item: (item.sort_order, item.field_key))
    if not 0 <= index < len(fields):
        return None
    return fields[index]


@router.callback_query(F.data.startswith("rr:"))
async def reveal_secret_short(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer()
        return
    rid = _parse_uuid(parts[1])
    try:
        index = int(parts[2])
    except ValueError:
        await callback.answer("Некорректное поле")
        return
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "VIEW", "record", rid):
            await callback.answer("Нет доступа к записи", show_alert=True)
            return
        record = await get_record(session, rid)
        if record is None:
            await callback.answer("Запись не найдена")
            return
        field = _resolve_field_by_index(record, index)
        if field is None or not field.encrypted_value:
            await callback.answer("Секрет не найден")
            return
        plain = decrypt_secret(field.encrypted_value)
        label = field.field_label
        await log_audit(session, user.id, "REVEAL_SECRET", "RecordField", str(field.id))

    # Раскрытие секрета не добавляется в историю просмотров: наружу уходит
    # только ephemeral-сообщение, значения в recent_* таблицах не сохраняются
    await callback.message.answer(
        f"🔑 {_escape(label)}:\n<code>{_escape(plain)}</code>",
        parse_mode="HTML",
    )
    await callback.answer("Показано. Секрет останется в истории чата.")


@router.callback_query(F.data.startswith("record:reveal:"))
async def reveal_secret(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer()
        return
    rid = _parse_uuid(parts[2])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    field_id = _parse_uuid(parts[3])
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "VIEW", "record", rid):
            await callback.answer("Нет доступа к записи", show_alert=True)
            return
        record = await get_record(session, rid)
        if not record:
            await callback.answer("Не найдено")
            return
        field = next((item for item in record.fields if item.id == field_id), None)
        if not field or not field.encrypted_value:
            await callback.answer("Нет секрета")
            return
        plain = decrypt_secret(field.encrypted_value)
        label = field.field_label
        await log_audit(session, user.id, "REVEAL_SECRET", "RecordField", str(field.id))

    # Раскрытие секрета не попадает в историю просмотров — см. rr: выше
    await callback.message.answer(
        f"🔑 {_escape(label)}:\n<code>{_escape(plain)}</code>",
        parse_mode="HTML",
    )
    await callback.answer("Показано. Секрет останется в истории чата.")


@router.callback_query(F.data.startswith("record:download:"))
async def download_file(callback: CallbackQuery) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "DOWNLOAD", "record", rid):
            await callback.answer("Нет прав на скачивание", show_alert=True)
            return
        record = await get_record(session, rid)
        if not record:
            await callback.answer("Запись не найдена")
            return
        field_map = {field.field_key: field.value_text for field in record.fields}
        bucket = field_map.get("bucket") or "office-vault"
        object_name = field_map.get("object_name")
        filename = field_map.get("filename") or record.title
        if not object_name:
            await callback.answer("Файл не найден")
            return

    from app.services.files.service import get_minio_client

    client = get_minio_client()
    try:
        response = client.get_object(bucket, object_name)
        data = response.read()
        response.close()
        response.release_conn()
    except Exception as exc:  # noqa: BLE001 — ошибка хранилища показывается пользователю
        await callback.answer(f"Ошибка скачивания: {exc}", show_alert=True)
        return

    from aiogram.types import BufferedInputFile

    await callback.message.answer_document(
        BufferedInputFile(data, filename=filename),
        caption=f"Файл: {record.title}",
    )
    await callback.answer("Файл отправлен")


# ---------------------------------------------------------------------------
# Редактирование записи
# ---------------------------------------------------------------------------


async def _render_edit_menu(
    target: Message | CallbackQuery, record, *, can_manage: bool = True
) -> None:
    fields = sorted(record.fields, key=lambda item: (item.sort_order, item.field_key))
    lines = [
        "✏️ <b>Редактирование</b>",
        "",
        f"Название: <b>{_escape(record.title)}</b>",
        f"Описание: {_escape(_clip(record.description or '—'))}",
        "",
        "<b>Поля:</b>",
    ]
    if fields:
        for field in fields:
            display = format_field_value(
                field.field_type,
                value_text=field.value_text,
                value_json=field.value_json,
                encrypted_value=field.encrypted_value,
            )
            lines.append(f"• {_escape(field.field_label)}: {_escape(_clip(display, 60))}")
    else:
        lines.append("—")
    rows = [
        [
            _button("✏️ Название", f"record:edit_title:{record.id}"),
            _button("📝 Описание", f"record:edit_desc:{record.id}"),
        ]
    ]
    for index, field in enumerate(fields):
        rows.append(
            [
                _button(
                    f"✏️ {field.field_label}",
                    f"record:edit_field:{record.id}:{index}",
                )
            ]
        )
    rows.append(
        [
            _button("➕ Добавить поле", f"record:edit_add:{record.id}"),
            _button("🏷 Теги", f"record:edit_tags:{record.id}"),
        ]
    )
    rows.append([_button("⬅️ К записи", f"record:view:{record.id}")])
    await _send(target, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^record:edit:[0-9a-fA-F-]{36}$"))
async def start_edit_record(callback: CallbackQuery, state: FSMContext) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await callback.answer("Нет прав на редактирование", show_alert=True)
            return
        record = await get_record(session, rid)
        if record is None:
            await callback.answer("Запись не найдена")
            return
    await state.clear()
    await _render_edit_menu(callback, record)
    await callback.answer()


@router.callback_query(F.data == "record:edit:cancel")
async def edit_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    rid = _parse_uuid(data.get("record_id"))
    if rid is None:
        await _send(
            callback,
            "Редактирование отменено.",
            InlineKeyboardMarkup(inline_keyboard=[[_button("🏠 В главное меню", "menu:main")]]),
        )
    else:
        await _send(
            callback,
            "Редактирование отменено.",
            InlineKeyboardMarkup(
                inline_keyboard=[[_button("📄 К записи", f"record:view:{rid}")]]
            ),
        )
    await callback.answer("Отменено")


@router.callback_query(F.data.startswith("record:edit_title:"))
async def edit_title_start(callback: CallbackQuery, state: FSMContext) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await callback.answer("Нет прав", show_alert=True)
            return
        record = await get_record(session, rid)
        if record is None:
            await callback.answer("Запись не найдена")
            return
        title = record.title

    await state.clear()
    await state.update_data(record_id=str(rid))
    await state.set_state(EditRecord.waiting_title)
    await _send(
        callback,
        f"Текущее название: <b>{_escape(title)}</b>\n\nВведите новое название:",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button("⏭ Пропустить", "record:edit:skip:title"),
                    _button("⬅️ Назад", f"record:edit:{rid}"),
                ],
                [_button("✖️ Отменить", "record:edit:cancel")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "record:edit:skip:title")
async def edit_skip_title(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    rid = _parse_uuid(data.get("record_id"))
    await state.clear()
    if rid is None:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    async with async_session() as session:
        record = await get_record(session, rid)
    if record is None:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    await _render_edit_menu(callback, record)
    await callback.answer()


@router.message(EditRecord.waiting_title)
async def edit_title(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rid = _parse_uuid(data.get("record_id"))
    if rid is None:
        await state.clear()
        await message.answer("Запись не найдена.")
        return
    raw = (message.text or "").strip()
    title = None
    if raw != "-":
        try:
            title = validate_record_title(raw)
        except RecordError as exc:
            await message.answer(f"❌ {_escape(exc)}\nВведите название ещё раз.")
            return

    async with async_session() as session:
        user = await _load_user(session, message.from_user.id)
        if not user:
            await state.clear()
            await message.answer("Ошибка авторизации.")
            return
        try:
            record = await update_record(session, rid, title=title, user_id=user.id)
        except RecordError as exc:
            await message.answer(f"❌ {_escape(exc)}")
            return
    await state.clear()
    if record is None:
        await message.answer("Запись не найдена.")
        return
    await message.answer(
        "✅ Название обновлено.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [_button("✏️ Редактировать ещё", f"record:edit:{rid}")],
                [_button("📄 К записи", f"record:view:{rid}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("record:edit_desc:"))
async def edit_description_start(callback: CallbackQuery, state: FSMContext) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await callback.answer("Нет прав", show_alert=True)
            return
        record = await get_record(session, rid)
        if record is None:
            await callback.answer("Запись не найдена")
            return
        description = record.description or "—"

    await state.clear()
    await state.update_data(record_id=str(rid))
    await state.set_state(EditRecord.waiting_description)
    await _send(
        callback,
        f"Текущее описание:\n{_escape(_clip(description, 500))}\n\n"
        "Введите новое описание («--» — очистить):",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button("⏭ Пропустить", "record:edit:skip:description"),
                    _button("⬅️ Назад", f"record:edit:{rid}"),
                ],
                [_button("✖️ Отменить", "record:edit:cancel")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "record:edit:skip:description")
async def edit_skip_description(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    rid = _parse_uuid(data.get("record_id"))
    await state.clear()
    if rid is None:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    async with async_session() as session:
        record = await get_record(session, rid)
    if record is None:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    await _render_edit_menu(callback, record)
    await callback.answer()


@router.message(EditRecord.waiting_description)
async def edit_description(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rid = _parse_uuid(data.get("record_id"))
    if rid is None:
        await state.clear()
        await message.answer("Запись не найдена.")
        return
    raw = (message.text or "").strip()
    if raw == "-":
        await state.clear()
        async with async_session() as session:
            record = await get_record(session, rid)
        if record:
            await _render_edit_menu(message, record)
        return
    description = "" if raw == "--" else raw
    try:
        description = validate_record_description(description)
    except RecordError as exc:
        await message.answer(f"❌ {_escape(exc)}\nВведите описание ещё раз.")
        return

    async with async_session() as session:
        user = await _load_user(session, message.from_user.id)
        if not user:
            await state.clear()
            await message.answer("Ошибка авторизации.")
            return
        try:
            record = await update_record(
                session, rid, description=description or "", user_id=user.id
            )
        except RecordError as exc:
            await message.answer(f"❌ {_escape(exc)}")
            return
    await state.clear()
    await message.answer(
        "✅ Описание обновлено.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [_button("✏️ Редактировать ещё", f"record:edit:{rid}")],
                [_button("📄 К записи", f"record:view:{rid}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("record:edit_field:"))
async def edit_field_start(callback: CallbackQuery, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer()
        return
    rid = _parse_uuid(parts[2])
    try:
        index = int(parts[3])
    except ValueError:
        await callback.answer("Некорректное поле")
        return
    if rid is None:
        await callback.answer("Некорректная запись")
        return

    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await callback.answer("Нет прав", show_alert=True)
            return
        record = await get_record(session, rid)
        if record is None:
            await callback.answer("Запись не найдена")
            return
        field = _resolve_field_by_index(record, index)
        if field is None:
            await callback.answer("Поле не найдено", show_alert=True)
            return
        field_type = normalize_field_type(field.field_type)
        current = format_field_value(
            field.field_type,
            value_text=field.value_text,
            value_json=field.value_json,
            encrypted_value=field.encrypted_value,
        )
        pending = {
            "field_id": str(field.id),
            "key": field.field_key,
            "label": field.field_label,
            "field_type": field_type.value,
            "required": False,
            "hint": FIELD_TYPE_HINTS[field_type],
        }

    await state.clear()
    await state.update_data(record_id=str(rid), pending_field=pending)
    await state.set_state(EditRecord.waiting_field_value)
    await _send(
        callback,
        f"Поле «{_escape(pending['label'])}»\n"
        f"Тип: {FIELD_TYPE_LABELS[field_type]}\n\n"
        f"Текущее значение: <code>{_escape(_clip(current, 300))}</code>\n\n"
        f"{_escape(FIELD_TYPE_HINTS[field_type])}",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button("⬅️ Назад", f"record:edit:{rid}"),
                    _button("✖️ Отменить", "record:edit:cancel"),
                ]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("record:edit_add:"))
async def edit_add_field_start(callback: CallbackQuery, state: FSMContext) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await callback.answer("Нет прав", show_alert=True)
            return
    await state.clear()
    await state.update_data(record_id=str(rid), pending_field=None)
    await state.set_state(EditRecord.waiting_field_name)
    await _send(
        callback,
        "Введите название нового поля:",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button("⬅️ Назад", f"record:edit:{rid}"),
                    _button("✖️ Отменить", "record:edit:cancel"),
                ]
            ]
        ),
    )
    await callback.answer()


@router.message(EditRecord.waiting_field_name)
async def edit_add_field_name(message: Message, state: FSMContext) -> None:
    label = (message.text or "").strip()
    if not label:
        await message.answer("Название поля не может быть пустым. Введите ещё раз.")
        return
    if len(label) > 128:
        await message.answer("Название поля не может быть длиннее 128 символов.")
        return
    data = await state.get_data()
    rid = _parse_uuid(data.get("record_id"))
    pending = {
        "key": f"field_{uuid4().hex[:8]}",
        "label": label,
        "field_type": None,
        "required": False,
        "hint": None,
    }
    await state.update_data(pending_field=pending)
    await state.set_state(EditRecord.waiting_field_type)
    await message.answer(
        f"Поле «{_escape(label)}». Выберите тип:",
        reply_markup=_type_keyboard("record:etype", f"record:edit:{rid}" if rid else "menu:main"),
    )


@router.callback_query(F.data.startswith("record:etype:"))
async def edit_add_field_type(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    pending = data.get("pending_field")
    if not pending:
        await callback.answer("Поле не выбрано", show_alert=True)
        return
    try:
        field_type = normalize_field_type((callback.data or "").split(":", 2)[-1])
    except FieldValidationError:
        await callback.answer("Неизвестный тип поля", show_alert=True)
        return
    pending.update(
        {
            "field_type": field_type.value,
            "hint": FIELD_TYPE_HINTS[field_type],
        }
    )
    rid = _parse_uuid(data.get("record_id"))
    await state.update_data(pending_field=pending)
    await state.set_state(EditRecord.waiting_field_value)
    await _send(
        callback,
        f"Поле «{_escape(pending['label'])}»\n"
        f"Тип: {FIELD_TYPE_LABELS[field_type]}\n\n"
        f"{_escape(FIELD_TYPE_HINTS[field_type])}",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button("⬅️ Назад", f"record:edit:{rid}" if rid else "menu:main"),
                    _button("✖️ Отменить", "record:edit:cancel"),
                ]
            ]
        ),
    )
    await callback.answer()


@router.message(EditRecord.waiting_field_value)
async def edit_field_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rid = _parse_uuid(data.get("record_id"))
    pending = data.get("pending_field")
    if rid is None or not pending:
        await state.clear()
        await message.answer("Сценарий устарел. Начните редактирование заново.")
        return

    field_type = normalize_field_type(pending.get("field_type"))
    try:
        value = validate_field_value(field_type, message.text or "")
    except FieldValidationError as exc:
        await message.answer(f"❌ {_escape(exc)}\n\n{FIELD_TYPE_HINTS[field_type]}")
        return
    if value is None:
        await message.answer("Значение не может быть пустым.")
        return

    async with async_session() as session:
        user = await _load_user(session, message.from_user.id)
        if not user:
            await state.clear()
            await message.answer("Ошибка авторизации.")
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await state.clear()
            await message.answer("Нет прав на редактирование.")
            return
        try:
            await set_record_field(
                session,
                rid,
                key=pending.get("key"),
                value=value,
                field_type=field_type,
                label=pending.get("label"),
                field_id=_parse_uuid(pending.get("field_id")),
                user_id=user.id,
            )
        except (RecordError, FieldValidationError) as exc:
            await message.answer(f"❌ {_escape(exc)}")
            return
    await state.clear()
    await message.answer(
        "✅ Поле обновлено.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [_button("✏️ Редактировать ещё", f"record:edit:{rid}")],
                [_button("📄 К записи", f"record:view:{rid}")],
            ]
        ),
    )


@router.message(EditRecord.waiting_field_type)
async def edit_field_type_hint(message: Message, state: FSMContext) -> None:
    await message.answer("Выберите тип поля кнопкой под сообщением.")


@router.callback_query(F.data.startswith("record:edit_tags:"))
async def edit_tags_start(callback: CallbackQuery, state: FSMContext) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await callback.answer("Нет прав", show_alert=True)
            return
        record = await get_record(session, rid)
        if record is None:
            await callback.answer("Запись не найдена")
            return
        current = ", ".join(link.tag.name for link in record.tags if link.tag)

    await state.clear()
    await state.update_data(record_id=str(rid))
    await state.set_state(EditRecord.waiting_tags)
    await _send(
        callback,
        "🏷 <b>Теги</b>\n\n"
        f"Текущие: {_escape(current) if current else '—'}\n\n"
        "Введите новые теги через запятую («--» — очистить):",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button("⬅️ Назад", f"record:edit:{rid}"),
                    _button("✖️ Отменить", "record:edit:cancel"),
                ]
            ]
        ),
    )
    await callback.answer()


@router.message(EditRecord.waiting_tags)
async def edit_tags(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rid = _parse_uuid(data.get("record_id"))
    if rid is None:
        await state.clear()
        await message.answer("Запись не найдена.")
        return
    raw = message.text or ""
    try:
        tags = [] if raw.strip() == "--" else _split_tags(raw)
    except FieldValidationError as exc:
        await message.answer(f"❌ {_escape(exc)}\nВведите теги ещё раз.")
        return
    async with async_session() as session:
        user = await _load_user(session, message.from_user.id)
        if not user:
            await state.clear()
            await message.answer("Ошибка авторизации.")
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await state.clear()
            await message.answer("Нет прав на редактирование.")
            return
        try:
            await update_record_tags(session, rid, tags, user_id=user.id)
        except (RecordError, FieldValidationError) as exc:
            await message.answer(f"❌ {_escape(exc)}")
            return
    await state.clear()
    await message.answer(
        "✅ Теги обновлены.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [_button("✏️ Редактировать ещё", f"record:edit:{rid}")],
                [_button("📄 К записи", f"record:view:{rid}")],
            ]
        ),
    )


# ---------------------------------------------------------------------------
# Архивация и удаление
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("record:archive:"))
async def do_archive(callback: CallbackQuery) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "ARCHIVE", "record", rid):
            await callback.answer("Нет прав", show_alert=True)
            return
        await archive_record(session, rid, user.id)
        await log_audit(session, user.id, "ARCHIVE_RECORD", "Record", str(rid))
    await callback.answer("Архивировано")
    await callback.message.edit_text("Запись перемещена в архив.")


@router.callback_query(F.data.startswith("record:delete:"))
async def do_delete(callback: CallbackQuery) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "DELETE", "record", rid):
            await callback.answer("Нет прав", show_alert=True)
            return
        await soft_delete_record(session, rid, user.id)
        await log_audit(session, user.id, "DELETE_RECORD", "Record", str(rid))
    await callback.answer("Удалено (мягко)")
    await callback.message.edit_text("Запись помечена как удалённая.")


# ---------------------------------------------------------------------------
# Phase 05: перемещение, восстановление, окончательное удаление и вложения
# ---------------------------------------------------------------------------

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # ограничение на размер присланного файла


class AttachFile(StatesGroup):
    """Прикрепление документа к существующей записи."""

    waiting_document = State()


def lifecycle_buttons(
    record,
    *,
    can_edit: bool = False,
    can_archive: bool = False,
    can_delete: bool = False,
) -> list[list[InlineKeyboardButton]]:
    """Кнопки жизненного цикла карточки (ТЗ §7): move, attachments, restore, purge.

    Для активных записей — архивация и мягкое удаление, для скрытых —
    восстановление и окончательное удаление. Все проверки прав дублируются
    в обработчиках: клавиатура только подсказывает доступные действия.
    """
    if not can_edit:
        return []
    rows: list[list[InlineKeyboardButton]] = [
        [_button("➡️ Переместить", f"record:move:{record.id}")],
        [_button("📎 Вложения", f"record:files:{record.id}")],
    ]
    if record.status == "ACTIVE":
        if can_archive:
            rows.append([_button("🗄 Архивировать", f"menu:ask:archive:{record.id}")])
        if can_delete:
            rows.append([_button("🗑 Удалить", f"menu:ask:delete:{record.id}")])
    else:
        if can_archive:
            rows.append([_button("♻️ Восстановить", f"record:unarchive:{record.id}")])
        if can_delete:
            rows.append([_button("☠️ Удалить окончательно", f"record:pdelete:{record.id}")])
    return rows


def _human_size(value: int | None) -> str:
    """Размер файла человекочитаемо."""
    size = float(value or 0)
    for unit in ("Б", "КБ", "МБ"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ГБ"


def _attachments_view(record, files: list[File], can_manage: bool) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура экрана вложений записи."""
    lines = ["📎 <b>Вложения</b>"]
    title = getattr(record, "title", None)
    if title:
        lines.append(_escape(title))
    lines.append("")
    if not files:
        lines.append("Файлов пока нет.")
    rows: list[list[InlineKeyboardButton]] = []
    for file in files:
        name = _clip(file.original_name, 40)
        lines.append(f"• {_escape(name)} · {_human_size(file.size)} · {_escape(file.mime_type)}")
        rows.append([_button(f"⬇️ {name}", f"record:file_down:{file.id}")])
        if can_manage:
            rows.append([_button(f"🗑 Удалить файл: {name[:28]}", f"record:file_del:{file.id}")])
    if can_manage:
        rows.append([_button("➕ Добавить файл", f"record:file_add:{record.id}")])
    rows.append([_button("⬅️ Назад", f"record:view:{record.id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("record:move"))
async def move_flow(callback: CallbackQuery, state: FSMContext) -> None:
    """Один вход для двух шагов: record:move:{rid} — выбор, record:move_to:{cid} — действие."""
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    if parts[1] == "move":
        await _move_screen(callback, state, parts[2])
    elif parts[1] == "move_to":
        await _move_apply(callback, state, parts[2])
    else:
        await callback.answer()


async def _move_screen(callback: CallbackQuery, state: FSMContext, rid_raw: str) -> None:
    rid = _parse_uuid(rid_raw)
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if rid is None or not await _has_permission(session, user, "EDIT", "record", rid):
            await callback.answer("Нет прав", show_alert=True)
            return
        record = await get_record(session, rid)
        if record is None:
            await callback.answer("Запись не найдена")
            return
        rows: list[list[InlineKeyboardButton]] = []
        for category in await list_categories(session):
            if category.id == record.category_id:
                continue
            if await _has_permission(session, user, "CREATE", "category", category.id):
                rows.append([_button(f"📁 {category.name[:28]}", f"record:move_to:{category.id}")])

    if not rows:
        await callback.answer("Нет категорий, доступных для перемещения", show_alert=True)
        return
    # UUID двух объектов не помещаются в callback_data, поэтому запись помним в FSM.
    await state.update_data(move_record_id=str(rid))
    rows.append([_button("⬅️ Назад", f"record:view:{rid}")])
    await callback.message.edit_text(
        f"➡️ Переместить запись «{_escape(record.title)}» в раздел:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


async def _move_apply(callback: CallbackQuery, state: FSMContext, cid_raw: str) -> None:
    target = _parse_uuid(cid_raw)
    data = await state.get_data()
    rid = _parse_uuid(data.get("move_record_id"))
    if rid is None or target is None:
        await callback.answer("Экран устарел, откройте карточку заново", show_alert=True)
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        try:
            record = await move_record(session, rid, target, user.id, user=user)
        except RecordError as e:
            await callback.answer(str(e), show_alert=True)
            return
        category = await get_category_by_id(session, target)
        await log_audit(
            session,
            user.id,
            "MOVE_RECORD",
            "Record",
            str(rid),
            metadata={"to_category": str(target)},
        )
    await state.update_data(move_record_id=None)
    title = category.name if category is not None else "новую категорию"
    await callback.answer("Перемещено")
    await callback.message.edit_text(
        f"✅ Запись «{_escape(record.title)}» перемещена в «{_escape(title)}».",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_button("📄 Открыть запись", f"record:view:{record.id}")]]
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("record:unarchive:"))
async def do_restore(callback: CallbackQuery) -> None:
    """Восстановление записи из архива или корзины."""
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "RESTORE", None, None):
            await callback.answer("Нет прав", show_alert=True)
            return
        try:
            record = await restore_record(session, rid, user.id, user=user)
        except RecordError as e:
            await callback.answer(str(e), show_alert=True)
            return
        if record is None:
            await callback.answer("Запись не найдена или не скрыта", show_alert=True)
            return
        await log_audit(session, user.id, "RESTORE_RECORD", "Record", str(rid))
    await callback.answer("Восстановлено")
    callback.data = f"record:view:{rid}"
    await view_record(callback)


@router.callback_query(F.data.startswith("record:pdelete:"))
async def ask_permanent_delete(callback: CallbackQuery) -> None:
    """Окончательное удаление возможно только после подтверждения."""
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "DELETE", None, None):
            await callback.answer("Нет прав", show_alert=True)
            return
        record = await get_record(session, rid)
        if record is None:
            await callback.answer("Запись не найдена")
            return
        if record.status not in PERMANENTLY_DELETABLE_STATUSES:
            await callback.answer(
                "Активную запись сначала нужно архивировать", show_alert=True
            )
            return

    await callback.message.edit_text(
        f"☠️ <b>Удалить запись «{_escape(record.title)}» окончательно?</b>\n\n"
        "Поля, теги и версии будут стёрты безвозвратно.",
        reply_markup=confirm_keyboard(f"record:pdelete_yes:{rid}", f"record:view:{rid}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("record:pdelete_yes:"))
async def do_permanent_delete(callback: CallbackQuery) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "DELETE", None, None):
            await callback.answer("Нет прав", show_alert=True)
            return
        try:
            await permanent_delete(session, rid, user.id, user=user)
        except RecordError as e:
            await callback.answer(str(e), show_alert=True)
            return
        await log_audit(session, user.id, "PERMANENT_DELETE_RECORD", "Record", str(rid))

    await callback.answer("Удалено окончательно")
    await callback.message.edit_text(
        "☠️ Запись удалена окончательно.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_button("🗃 К архиву", "archive:open")]]
        ),
    )


@router.callback_query(F.data.startswith("record:files:"))
async def attachments_screen(callback: CallbackQuery, state: FSMContext) -> None:
    """Список вложений записи (ТЗ: просмотр и управление файлами)."""
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        can_manage = await _has_permission(session, user, "EDIT", "record", rid)
        if not can_manage and not await _has_permission(session, user, "VIEW", "record", rid):
            await callback.answer("Нет доступа к записи", show_alert=True)
            return
        record = await get_record(session, rid)
        if record is None:
            await callback.answer("Запись не найдена")
            return
        files = await list_attachments(session, rid)
        if can_manage:
            # Файл и запись вместе не влезают в callback_data — помним запись в FSM.
            await state.update_data(attach_record_id=str(rid))

    text, markup = _attachments_view(record, files, can_manage=can_manage)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("record:file_down:"))
async def download_attachment(callback: CallbackQuery) -> None:
    """Скачивание вложения по праву DOWNLOAD на файл (файл доступен через связанные записи)."""
    fid = _parse_uuid((callback.data or "").split(":")[-1])
    if fid is None:
        await callback.answer("Некорректный файл")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        file = await session.get(File, fid)
        if file is None or file.deleted_at is not None:
            await callback.answer("Файл не найден", show_alert=True)
            return
        if not await _has_permission(session, user, "DOWNLOAD", "file", fid):
            await callback.answer("Нет доступа к файлу", show_alert=True)
            return
        bucket, storage_key, name = file.bucket, file.storage_key, file.original_name
        await log_audit(session, user.id, "DOWNLOAD_FILE", "File", str(fid))

    from aiogram.types import BufferedInputFile

    from app.services.files.service import get_minio_client

    try:
        client = get_minio_client()
        response = client.get_object(bucket, storage_key)
        try:
            data = response.read()
        finally:
            response.close()
            response.release_conn()
    except Exception as e:  # noqa: BLE001 — сеть/хранилище недоступны
        await callback.answer(f"Ошибка скачивания: {e}", show_alert=True)
        return

    await callback.message.answer_document(
        BufferedInputFile(data, filename=name),
        caption=f"📎 {name}",
    )
    await callback.answer("Файл отправлен")


@router.callback_query(F.data.startswith("record:file_add:"))
async def start_attachment_upload(callback: CallbackQuery, state: FSMContext) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await callback.answer("Нет прав", show_alert=True)
            return

    await state.update_data(attach_record_id=str(rid))
    await state.set_state(AttachFile.waiting_document)
    await callback.message.answer("Отправьте файл-документ. Для отмены — /cancel.")
    await callback.answer()


async def _store_attachment(session, *, raw: bytes, doc, uploader_id: UUID) -> File:
    """Складывает байты в MinIO и создаёт метаданные файла.

    Проверки содержимого (magic bytes, антивирус, quarantine) — Phase 06.
    """
    from app.services.files.service import get_minio_client

    bucket = getattr(settings, "minio_bucket", None) or "office-vault"
    client = get_minio_client()
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)

    filename = doc.file_name or "document.bin"
    storage_key = f"attachments/{uuid4()}/{filename}"
    mime_type = doc.mime_type or "application/octet-stream"
    client.put_object(
        bucket,
        storage_key,
        BytesIO(raw),
        length=len(raw),
        content_type=mime_type,
    )

    extension = filename.rsplit(".", 1)[1].lower()[:32] if "." in filename else None
    file = File(
        original_name=filename[:512],
        storage_key=storage_key[:1024],
        bucket=bucket,
        mime_type=mime_type[:128],
        extension=extension,
        size=doc.file_size or len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        uploaded_by=uploader_id,
    )
    session.add(file)
    await session.flush()
    return file


@router.message(AttachFile.waiting_document, F.document)
async def receive_attachment(message: Message, state: FSMContext) -> None:
    """Загрузка документа с привязкой к записи (ТЗ: «Добавить файл»)."""
    data = await state.get_data()
    rid = _parse_uuid(data.get("attach_record_id"))
    await state.clear()
    if rid is None:
        await message.answer("Экран устарел — откройте карточку записи заново.")
        return

    doc = message.document
    if doc.file_size and doc.file_size > MAX_ATTACHMENT_BYTES:
        await message.answer("Файл слишком большой (максимум 25 МБ).")
        return

    path = await message.bot.download(doc)
    raw = path.read_bytes()

    async with async_session() as session:
        user = await _load_user(session, message.from_user.id)
        if not await is_user_active(user):
            await message.answer("Нет доступа")
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await message.answer("Нет прав на изменение этой записи.")
            return
        try:
            file = await _store_attachment(session, raw=raw, doc=doc, uploader_id=user.id)
            await add_attachment(session, rid, file.id)
        except RecordError as e:
            await message.answer(f"Ошибка: {e}")
            return
        except Exception as e:  # noqa: BLE001 — MinIO/сеть недоступны
            await message.answer(f"Не удалось сохранить файл: {e}")
            return
        await log_audit(
            session,
            user.id,
            "ATTACH_FILE",
            "Record",
            str(rid),
            metadata={"file_id": str(file.id)},
        )

    await message.answer(
        f"✅ Файл «{_escape(doc.file_name or 'document')}» прикреплён к записи.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_button("📎 Вложения", f"record:files:{rid}")]]
        ),
        parse_mode="HTML",
    )


@router.message(AttachFile.waiting_document)
async def attachment_wrong_input(message: Message, state: FSMContext) -> None:
    await message.answer("Нужно отправить файл-документ. Для отмены — /cancel.")


@router.callback_query(F.data.startswith("record:file_del:"))
async def remove_attachment_link(callback: CallbackQuery, state: FSMContext) -> None:
    """Отвязка файла от записи (сам файл остаётся в хранилище до Phase 06)."""
    fid = _parse_uuid((callback.data or "").split(":")[-1])
    data = await state.get_data()
    rid = _parse_uuid(data.get("attach_record_id"))
    if rid is None or fid is None:
        await callback.answer("Список устарел — откройте вложения заново", show_alert=True)
        return

    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await callback.answer("Нет прав", show_alert=True)
            return
        removed = await remove_attachment(session, rid, fid)
        record = await get_record(session, rid)
        if record is None:
            await callback.answer("Запись не найдена")
            return
        files = await list_attachments(session, rid)
        await log_audit(
            session,
            user.id,
            "DETACH_FILE",
            "Record",
            str(rid),
            metadata={"file_id": str(fid)},
        )

    text, markup = _attachments_view(record, files, can_manage=True)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer("Файл отвязан" if removed else "Файл уже был отвязан")


# ---------------------------------------------------------------------------
# История версий и теги (Phase 05)
# ---------------------------------------------------------------------------


def _snapshot_field_text(item: dict) -> str:
    if item.get("encrypted_value"):
        return SECRET_MASK
    field_type = item.get("field_type", FieldType.TEXT.value)
    if item.get("value_json") is not None:
        return format_raw_field_value(field_type, item["value_json"])
    return format_raw_field_value(field_type, item.get("value_text"))


def _version_line(version) -> str:
    when = version.created_at.strftime("%d.%m.%Y %H:%M") if version.created_at else "—"
    note = _escape(_clip(version.change_description or "изменение", 80))
    return f"• v{version.version} · {when} — {note}"


@router.callback_query(F.data.startswith("record:history:"))
async def show_history(callback: CallbackQuery) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "VIEW", "record", rid):
            await callback.answer("Нет доступа к записи", show_alert=True)
            return
        record = await get_record(session, rid)
        if record is None:
            await callback.answer("Запись не найдена")
            return
        versions = await list_versions(rid, session)
        can_edit = await _has_permission(session, user, "EDIT", "record", rid)

    lines = [f"📝 <b>История: {_escape(record.title)}</b>", ""]
    if versions:
        lines.extend(_version_line(version) for version in versions[:10])
    else:
        lines.append("Версий пока нет.")
    rows = [
        [_button(f"👁 v{version.version}", f"record:version:{rid}:{version.version}")]
        for version in versions[:10]
    ]
    if can_edit:
        rows.append([_button("🏷 Теги", f"record:tags:{rid}")])
    rows.append([_button("⬅️ К записи", f"record:view:{rid}")])
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("record:version:"))
async def show_version(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    rid = _parse_uuid(parts[2]) if len(parts) > 2 else None
    try:
        number = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        number = 0
    if rid is None or number < 1:
        await callback.answer("Некорректная версия")
        return

    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "VIEW", "record", rid):
            await callback.answer("Нет доступа к записи", show_alert=True)
            return
        version = await get_version(rid, number, session)
        if version is None:
            await callback.answer("Версия не найдена", show_alert=True)
            return
        can_edit = await _has_permission(session, user, "EDIT", "record", rid)
        changes = []
        if number > 1:
            try:
                changes = await get_version_diff(rid, number - 1, number, session)
            except RecordVersionError:
                changes = []

    snapshot = version.snapshot or {}
    lines = [f"📝 <b>Версия {number}</b>", ""]
    lines.append(f"Название: <b>{_escape(snapshot.get('title') or '—')}</b>")
    lines.append(f"Описание: {_escape(_clip(snapshot.get('description') or '—'))}")
    lines.append("")
    lines.append("<b>Поля:</b>")
    snapshot_fields = snapshot.get("fields") or []
    if snapshot_fields:
        for item in snapshot_fields:
            lines.append(
                f"• {_escape(item.get('field_label', item.get('field_key', 'Поле')))}: "
                f"{_escape(_clip(_snapshot_field_text(item)))}"
            )
    else:
        lines.append("—")
    if changes:
        lines += ["", "<b>Отличия от предыдущей версии:</b>"]
        for change in changes[:15]:
            if change.secret:
                lines.append(f"• {_escape(change.label)}: секрет изменён")
            else:
                lines.append(
                    f"• {_escape(change.label)}: "
                    f"{_escape(_clip(change.old or '—', 40))} → "
                    f"{_escape(_clip(change.new or '—', 40))}"
                )
    rows = []
    if can_edit:
        rows.append(
            [
                _button(
                    "↩️ Восстановить версию",
                    f"record:restore:{rid}:{number}",
                )
            ]
        )
    rows.append([_button("⬅️ К истории", f"record:history:{rid}")])
    rows.append([_button("📄 К записи", f"record:view:{rid}")])
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("record:restore:"))
async def restore_version_callback(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    rid = _parse_uuid(parts[2]) if len(parts) > 2 else None
    try:
        number = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        number = 0
    if rid is None or number < 1:
        await callback.answer("Некорректная версия")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await callback.answer("Нет прав", show_alert=True)
            return
        try:
            await restore_version(rid, number, session, changed_by=user.id)
        except RecordVersionError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await log_audit(
            session,
            user.id,
            "RESTORE_VERSION",
            "Record",
            str(rid),
            metadata={"version": number},
        )
    await callback.answer(f"Версия {number} восстановлена")
    callback.data = f"record:view:{rid}"
    await view_record(callback)


async def _render_tags_screen(callback: CallbackQuery, rid: UUID) -> bool:
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return False
        if not await _has_permission(session, user, "VIEW", "record", rid):
            await callback.answer("Нет доступа к записи", show_alert=True)
            return False
        record = await get_record(session, rid)
        if record is None:
            await callback.answer("Запись не найдена", show_alert=True)
            return False
        can_edit = await _has_permission(session, user, "EDIT", "record", rid)
        attached = await get_record_tags(rid, session)
        all_tags = await list_tags(session)

    attached_ids = {tag.id for tag in attached}
    lines = [f"🏷 <b>Теги: {_escape(record.title)}</b>", ""]
    lines.append("Назначены: " + (_escape(", ".join(tag.name for tag in attached)) or "—"))
    available = [tag for tag in all_tags if tag.id not in attached_ids][:20]
    if available:
        lines += ["", "Доступные теги:"]
        lines.extend(f"• {_escape(tag.name)}" for tag in available)
    rows: list[list[InlineKeyboardButton]] = []
    if can_edit:
        for tag in attached:
            rows.append(
                [
                    _button(
                        f"✖️ {_clip(tag.name, 40)}",
                        f"record:tagdel:{rid}:{tag_key(tag)}",
                    )
                ]
            )
        for tag in available:
            rows.append(
                [
                    _button(
                        f"➕ {_clip(tag.name, 40)}",
                        f"record:tagadd:{rid}:{tag_key(tag)}",
                    )
                ]
            )
        rows.append([_button("➕ Новый тег", f"record:tagnew:{rid}")])
    rows.append([_button("⬅️ К записи", f"record:view:{rid}")])
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    return True


@router.callback_query(F.data.startswith("record:tags:"))
async def manage_tags(callback: CallbackQuery) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    if await _render_tags_screen(callback, rid):
        await callback.answer()


@router.callback_query(F.data.startswith("record:tag:"))
async def pick_tag(callback: CallbackQuery) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    if await _render_tags_screen(callback, rid):
        await callback.answer()


async def _tags_record_id(callback: CallbackQuery) -> UUID | None:
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer()
        return None
    rid = _parse_uuid(parts[2])
    if rid is None:
        await callback.answer("Некорректная запись")
        return None
    return rid


@router.callback_query(F.data.startswith("record:tagadd:"))
async def attach_tag(callback: CallbackQuery) -> None:
    rid = await _tags_record_id(callback)
    if rid is None:
        return
    key = (callback.data or "").split(":")[-1]
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await callback.answer("Нет прав", show_alert=True)
            return
        tag = await get_tag_by_key(key, session)
        if tag is None:
            await callback.answer("Тег не найден", show_alert=True)
            return
        try:
            added = await add_tag_to_record(rid, tag.id, session)
        except TagError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await log_audit(
            session,
            user.id,
            "ATTACH_TAG",
            "Record",
            str(rid),
            metadata={"tag": tag.name},
        )
    if await _render_tags_screen(callback, rid):
        await callback.answer("Тег добавлен" if added else "Тег уже назначен")


@router.callback_query(F.data.startswith("record:tagdel:"))
async def detach_tag(callback: CallbackQuery) -> None:
    rid = await _tags_record_id(callback)
    if rid is None:
        return
    key = (callback.data or "").split(":")[-1]
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await callback.answer("Нет прав", show_alert=True)
            return
        tag = await get_tag_by_key(key, session)
        if tag is None:
            await callback.answer("Тег не найден", show_alert=True)
            return
        removed = await remove_tag_from_record(rid, tag.id, session)
        await log_audit(
            session,
            user.id,
            "DETACH_TAG",
            "Record",
            str(rid),
            metadata={"tag": tag.name},
        )
    if await _render_tags_screen(callback, rid):
        await callback.answer("Тег убран" if removed else "Тега не было")


class ManageTags(StatesGroup):
    waiting_name = State()


@router.callback_query(F.data.startswith("record:tagnew:"))
async def start_new_tag(callback: CallbackQuery, state: FSMContext) -> None:
    rid = _parse_uuid((callback.data or "").split(":")[-1])
    if rid is None:
        await callback.answer("Некорректная запись")
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await callback.answer("Нет прав", show_alert=True)
            return
    await state.clear()
    await state.update_data(tag_record_id=str(rid))
    await state.set_state(ManageTags.waiting_name)
    await callback.message.answer(
        "Введите название нового тега (можно добавить цвет: «срочно #ff8800»):",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_button("✖️ Отменить", f"record:tags:{rid}")]]
        ),
    )
    await callback.answer()


@router.message(ManageTags.waiting_name)
async def new_tag_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rid = _parse_uuid(data.get("tag_record_id"))
    if rid is None:
        await state.clear()
        await message.answer("Запись не найдена.")
        return

    raw = (message.text or "").strip()
    color = None
    name = raw
    if " " in raw:
        candidate_name, candidate_color = raw.rsplit(" ", 1)
        if candidate_color.startswith("#"):
            name, color = candidate_name, candidate_color

    async with async_session() as session:
        user = await _load_user(session, message.from_user.id)
        if not await is_user_active(user):
            await state.clear()
            await message.answer("Нет доступа.")
            return
        if not await _has_permission(session, user, "EDIT", "record", rid):
            await state.clear()
            await message.answer("Нет прав на редактирование.")
            return
        try:
            tag = await create_tag(name, color, session)
            await add_tag_to_record(rid, tag.id, session)
        except TagError as exc:
            await message.answer(f"❌ {_escape(exc)}\nВведите другое название.")
            return
        await log_audit(
            session,
            user.id,
            "CREATE_TAG",
            "Record",
            str(rid),
            metadata={"tag": tag.name},
        )

    await state.clear()
    await message.answer(
        f"✅ Тег «{_escape(tag.name)}» добавлен.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [_button("🏷 К тегам", f"record:tags:{rid}")],
                [_button("📄 К записи", f"record:view:{rid}")],
            ]
        ),
        parse_mode="HTML",
    )


__all__ = ["router"]
