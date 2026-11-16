from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    AccessMode,
    Category,
    Favorite,
    FieldType,
    File,
    Group,
    Permission,
    PermissionCode,
    Record,
    RecordField,
    RecordFile,
    RecordGroupPermission,
    RecordPermission,
    RecordTag,
    RecordType,
    Tag,
    User,
)
from app.services.permissions import PermissionService
from app.services.records.fields import (
    DEFAULT_FIELD_DEFINITIONS,
    EncodedField,
    FieldDefinition,
    FieldInput,
    MissingRequiredField,
    default_field_definitions,
    encode_field_value,
    field_definition_map,
    humanize_field_label,
    is_empty_value,
    normalize_field_key,
    normalize_field_label,
    normalize_field_type,
    normalize_record_type,
    validate_field_value,
    validate_required_fields,
)

MAX_TITLE_LENGTH = 256
MAX_DESCRIPTION_LENGTH = 20000
MAX_TAG_LENGTH = 64


class RecordError(ValueError):
    """Базовая ошибка домена записей."""


class RecordNotFound(RecordError):
    """Запись не найдена."""


class RecordValidationError(RecordError):
    """Некорректные данные записи."""


class RecordPermissionError(RecordError):
    """Недостаточно прав для операции с записью (перемещение, удаление, вложения)."""


class RecordStateError(RecordError):
    """Текущий статус записи не допускает операцию."""


# Окончательно удалять и восстанавливать можно только скрытые записи.
RESTORABLE_STATUSES = frozenset({"ARCHIVED", "DELETED"})
PERMANENTLY_DELETABLE_STATUSES = frozenset({"ARCHIVED", "DELETED"})


def validate_record_title(title: str) -> str:
    normalized = str(title or "").strip()
    if not normalized:
        raise RecordValidationError("Название записи не может быть пустым.")
    if len(normalized) > MAX_TITLE_LENGTH:
        raise RecordValidationError(
            f"Название записи не может быть длиннее {MAX_TITLE_LENGTH} символов."
        )
    return normalized


def validate_record_description(description: str | None) -> str | None:
    if description is None:
        return None
    normalized = str(description).strip()
    if len(normalized) > MAX_DESCRIPTION_LENGTH:
        raise RecordValidationError(
            f"Описание не может быть длиннее {MAX_DESCRIPTION_LENGTH} символов."
        )
    return normalized or None


def normalize_access_mode(value: AccessMode | str) -> AccessMode:
    if isinstance(value, AccessMode):
        return value
    try:
        return AccessMode(str(value).strip().upper())
    except ValueError as exc:
        raise RecordValidationError(f"Неизвестный режим доступа: {value}.") from exc


async def update_record(
    session: AsyncSession,
    record_id: UUID,
    *,
    title: str | None = None,
    description: str | None = None,
    user_id: UUID | None = None,
) -> Record | None:
    record = await get_record(session, record_id)
    if not record:
        return None
    if title is not None:
        record.title = validate_record_title(title)
    if description is not None:
        record.description = validate_record_description(description)
    record.version = (record.version or 1) + 1
    if user_id:
        record.updated_by = user_id
    await session.commit()
    await session.refresh(record)
    return record


async def get_record(session: AsyncSession, record_id: UUID) -> Record | None:
    result = await session.execute(
        select(Record)
        .where(Record.id == record_id)
        .options(
            selectinload(Record.fields),
            selectinload(Record.tags).selectinload(RecordTag.tag),
        )
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def archive_record(session: AsyncSession, record_id: UUID, user_id: UUID) -> None:
    record = await get_record(session, record_id)
    if record:
        record.status = "ARCHIVED"
        record.updated_by = user_id
        await session.commit()


async def soft_delete_record(session: AsyncSession, record_id: UUID, user_id: UUID) -> None:
    record = await get_record(session, record_id)
    if record:
        record.status = "DELETED"
        record.updated_by = user_id
        await session.commit()


async def restore_record(
    session: AsyncSession,
    record_id: UUID,
    user_id: UUID,
    *,
    user: User | None = None,
) -> Record | None:
    """Восстановление записи из архива или корзины.

    Возвращает None, если запись не найдена или не скрыта. Если передан
    пользователь, проверяется системное право RESTORE (Default Deny).
    """
    record = await get_record(session, record_id)
    if record is None or record.status not in RESTORABLE_STATUSES:
        return None
    if user is not None and not await PermissionService.can(
        user, PermissionCode.RESTORE, None, None, session
    ):
        raise RecordPermissionError("Нет права восстанавливать записи.")
    record.status = "ACTIVE"
    record.updated_by = user_id
    await session.commit()
    await session.refresh(record)
    return record


async def get_category_by_code(session: AsyncSession, code: str) -> Category | None:
    result = await session.execute(select(Category).where(Category.code == code))
    return result.scalar_one_or_none()


async def list_records(
    session: AsyncSession,
    category_code: str,
    record_type: RecordType,
    limit: int = 20,
) -> list[Record]:
    category = await get_category_by_code(session, category_code)
    if not category:
        return []
    result = await session.execute(
        select(Record)
        .where(Record.category_id == category.id, Record.record_type == record_type, Record.status == "ACTIVE")
        .options(selectinload(Record.fields))
        .order_by(Record.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_records_page(
    session: AsyncSession,
    category_code: str,
    *,
    record_type: RecordType | None = None,
    sort: str = "date",
    offset: int = 0,
    limit: int = 10,
) -> tuple[list[Record], int]:
    """Страница активных записей категории и общее количество (для пагинации)."""
    category = await get_category_by_code(session, category_code)
    if not category:
        return [], 0

    conditions = [Record.category_id == category.id, Record.status == "ACTIVE"]
    if record_type is not None:
        conditions.append(Record.record_type == record_type)

    total = await session.scalar(select(func.count()).select_from(Record).where(*conditions))

    order = (
        func.lower(Record.title).asc() if sort == "name" else Record.created_at.desc()
    )
    result = await session.execute(
        select(Record)
        .where(*conditions)
        .options(selectinload(Record.fields))
        .order_by(order, Record.id)
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), total or 0


def _field_input_from_value(
    raw_key: str,
    raw_value: Any,
    *,
    definitions: Mapping[str, FieldDefinition],
    secret_fields: set[str],
) -> FieldInput:
    """Приводит значение из FSM, CSV или вызова сервиса к FieldInput."""
    key = normalize_field_key(raw_key)
    default = definitions.get(key)

    if isinstance(raw_value, FieldInput):
        field_type = normalize_field_type(raw_value.field_type)
        label = raw_value.label or (default.label if default else humanize_field_label(key))
        return FieldInput(
            key=key,
            value=raw_value.value,
            field_type=field_type,
            label=label,
            encrypted=raw_value.encrypted,
        )

    is_descriptor = isinstance(raw_value, Mapping) and (
        raw_value.get("__field__") is True
        or "field_type" in raw_value
        or "encrypted" in raw_value
    )
    if is_descriptor:
        raw_type = raw_value.get("field_type")
        field_type = (
            normalize_field_type(raw_type)
            if raw_type
            else default.field_type
            if default
            else FieldType.PASSWORD
            if key in secret_fields
            else FieldType.TEXT
        )
        label = raw_value.get("label") or (
            default.label if default else humanize_field_label(key)
        )
        value = raw_value.get("value")
        if value is None:
            value = raw_value.get("encrypted_value")
        return FieldInput(
            key=key,
            value=value,
            field_type=field_type,
            label=normalize_field_label(str(label)),
            encrypted=bool(raw_value.get("encrypted")),
        )

    field_type = (
        default.field_type
        if default
        else FieldType.PASSWORD
        if key in secret_fields
        else FieldType.TEXT
    )
    label = default.label if default else humanize_field_label(key)
    return FieldInput(key=key, value=raw_value, field_type=field_type, label=label)


def _encode_input(item: FieldInput) -> tuple[EncodedField, Any]:
    if item.encrypted:
        if not item.value:
            return EncodedField(raw_value=None), None
        encrypted = str(item.value)
        return EncodedField(encrypted_value=encrypted, raw_value=None), encrypted
    normalized = validate_field_value(item.field_type, item.value)
    if normalized is None:
        return EncodedField(raw_value=None), None
    return encode_field_value(item.field_type, normalized), normalized


async def _attach_tags(
    session: AsyncSession,
    record_id: UUID,
    tags: Sequence[str] | None,
) -> None:
    if not tags:
        return
    names: list[str] = []
    for raw in tags:
        name = str(raw or "").strip().lower()
        if not name:
            continue
        if len(name) > MAX_TAG_LENGTH:
            raise RecordValidationError(
                f"Тег не может быть длиннее {MAX_TAG_LENGTH} символов."
            )
        if name not in names:
            names.append(name)
    if not names:
        return

    result = await session.execute(select(Tag).where(Tag.name.in_(names)))
    existing = {tag.name: tag for tag in result.scalars().all()}
    ordered: list[Tag] = []
    for name in names:
        tag = existing.get(name)
        if tag is None:
            tag = Tag(name=name)
            session.add(tag)
            existing[name] = tag
        ordered.append(tag)
    await session.flush()
    for tag in ordered:
        session.add(RecordTag(record_id=record_id, tag_id=tag.id))


async def _replace_tags(
    session: AsyncSession,
    record_id: UUID,
    tags: Sequence[str] | None,
) -> None:
    await session.execute(delete(RecordTag).where(RecordTag.record_id == record_id))
    await _attach_tags(session, record_id, tags)


def _attachment_entries(attachments: Sequence[Any] | None) -> list[tuple[str, str, EncodedField]]:
    entries: list[tuple[str, str, EncodedField]] = []
    for idx, attachment in enumerate(attachments or (), start=1):
        if is_empty_value(attachment):
            continue
        encoded = encode_field_value(FieldType.FILE_REFERENCE, attachment)
        if encoded.raw_value is None:
            continue
        label = "Файл"
        if isinstance(attachment, Mapping):
            label = str(attachment.get("filename") or f"Файл {idx}")
        elif isinstance(attachment, str):
            label = attachment.rsplit("/", 1)[-1] or f"Файл {idx}"
        entries.append((f"attachment_{idx}", label, encoded))
    return entries


async def _apply_record_permission(
    session: AsyncSession,
    record_id: UUID,
    *,
    access_mode: AccessMode | None,
    group_codes: Sequence[str] | None,
) -> None:
    if access_mode is None and not group_codes:
        return

    mode = access_mode or AccessMode.GROUP
    session.add(RecordPermission(record_id=record_id, access_mode=mode))
    if not group_codes:
        return

    normalized_codes = []
    for code in group_codes:
        value = str(code or "").strip()
        if value and value not in normalized_codes:
            normalized_codes.append(value)
    if not normalized_codes:
        return

    result = await session.execute(select(Group).where(Group.code.in_(normalized_codes)))
    groups = {group.code: group for group in result.scalars().all()}
    missing = [code for code in normalized_codes if code not in groups]
    if missing:
        raise RecordValidationError(f"Группы не найдены: {', '.join(missing)}.")

    permission = await session.scalar(
        select(Permission).where(Permission.code == PermissionCode.VIEW.value)
    )
    if permission is None:
        raise RecordValidationError("Системное право VIEW не настроено.")
    for code in normalized_codes:
        session.add(
            RecordGroupPermission(
                record_id=record_id,
                group_id=groups[code].id,
                permission_id=permission.id,
                granted=True,
            )
        )


async def create_record(
    session: AsyncSession,
    *,
    category_code: str,
    record_type: RecordType,
    title: str,
    description: str | None,
    created_by: User,
    fields: Mapping[str, Any] | None = None,
    secret_fields: set[str] | None = None,
    field_definitions: Sequence[FieldDefinition] | None = None,
    validate_required: bool = True,
    tags: Sequence[str] | None = None,
    attachments: Sequence[Any] | None = None,
    access_mode: AccessMode | str | None = None,
    group_codes: Sequence[str] | None = None,
    commit: bool = True,
) -> Record:
    """Создаёт запись со всеми типами динамических полей.

    ``commit=False`` откладывает фиксацию: вызывающий код (например,
    фоновый импорт Phase 08) управляет транзакцией сам и может откатить
    все строки до коммита.
    """
    category = await get_category_by_code(session, category_code)
    if not category:
        raise RecordValidationError(f"Категория {category_code} не найдена.")

    normalized_type = normalize_record_type(record_type)
    normalized_title = validate_record_title(title)
    normalized_description = validate_record_description(description)
    definitions = (
        {item.key: item for item in field_definitions}
        if field_definitions is not None
        else field_definition_map(normalized_type)
    )
    secrets = secret_fields or set()

    prepared: list[tuple[FieldInput, EncodedField]] = []
    required_values: dict[str, Any] = {}
    seen_keys: set[str] = set()
    for raw_key, raw_value in (fields or {}).items():
        item = _field_input_from_value(
            str(raw_key), raw_value, definitions=definitions, secret_fields=secrets
        )
        if item.key in seen_keys:
            raise RecordValidationError(f"Поле «{item.key}» указано дважды.")
        seen_keys.add(item.key)
        encoded, normalized = _encode_input(item)
        if encoded.raw_value is None and not encoded.encrypted_value:
            continue
        prepared.append((item, encoded))
        if normalized is not None or item.encrypted:
            required_values[item.key] = normalized if normalized is not None else item.value

    if validate_required:
        validate_required_fields(
            normalized_type,
            required_values,
            definitions=tuple(definitions.values()) if definitions else default_field_definitions(normalized_type),
        )

    mode = normalize_access_mode(access_mode) if access_mode is not None else None
    if group_codes and mode is None:
        mode = AccessMode.GROUP

    record = Record(
        category_id=category.id,
        record_type=normalized_type,
        title=normalized_title,
        description=normalized_description,
        created_by=created_by.id,
        updated_by=created_by.id,
    )
    if mode is not None:
        record.access_mode = mode
    session.add(record)
    await session.flush()

    sort_order = 0
    for item, encoded in prepared:
        if encoded.raw_value is None and not encoded.encrypted_value:
            continue
        session.add(
            RecordField(
                record_id=record.id,
                field_key=item.key,
                field_label=normalize_field_label(item.label or humanize_field_label(item.key)),
                field_type=item.field_type.value,
                value_text=encoded.value_text,
                value_json=encoded.value_json,
                encrypted_value=encoded.encrypted_value,
                sort_order=sort_order,
            )
        )
        sort_order += 1

    for key, label, encoded in _attachment_entries(attachments):
        if key in seen_keys:
            continue
        session.add(
            RecordField(
                record_id=record.id,
                field_key=key,
                field_label=normalize_field_label(label, fallback="Файл"),
                field_type=FieldType.FILE_REFERENCE.value,
                value_text=encoded.value_text,
                value_json=encoded.value_json,
                encrypted_value=encoded.encrypted_value,
                sort_order=sort_order,
            )
        )
        sort_order += 1

    await _attach_tags(session, record.id, tags)
    await _apply_record_permission(
        session, record.id, access_mode=mode, group_codes=group_codes
    )

    if commit:
        await session.commit()
    return await get_record(session, record.id) or record


async def set_record_field(
    session: AsyncSession,
    record_id: UUID,
    *,
    key: str | None = None,
    value: Any = None,
    field_type: FieldType | str | None = None,
    label: str | None = None,
    field_id: UUID | None = None,
    user_id: UUID | None = None,
    encrypted: bool = False,
) -> RecordField:
    """Создаёт или обновляет одно динамическое поле записи."""
    record = await get_record(session, record_id)
    if record is None:
        raise RecordNotFound("Запись не найдена.")

    existing = None
    if field_id is not None:
        existing = next((item for item in record.fields if item.id == field_id), None)
        if existing is None:
            raise RecordNotFound("Поле записи не найдено.")
    elif key is not None:
        normalized_key = normalize_field_key(key)
        existing = next((item for item in record.fields if item.field_key == normalized_key), None)
    else:
        raise RecordValidationError("Укажите ключ или ID поля.")

    if existing is not None:
        item_key = existing.field_key
        resolved_type = (
            normalize_field_type(field_type)
            if field_type is not None
            else normalize_field_type(existing.field_type)
        )
        resolved_label = label or existing.field_label
    else:
        if key is None:
            raise RecordValidationError("Укажите ключ нового поля.")
        item_key = normalize_field_key(key)
        definition = field_definition_map(record.record_type).get(item_key)
        resolved_type = (
            normalize_field_type(field_type)
            if field_type is not None
            else definition.field_type
            if definition
            else FieldType.TEXT
        )
        resolved_label = label or (
            definition.label if definition else humanize_field_label(item_key)
        )

    if encrypted:
        if not value:
            raise RecordValidationError("Значение секретного поля не может быть пустым.")
        encoded = EncodedField(encrypted_value=str(value))
    else:
        encoded = encode_field_value(resolved_type, value)

    if existing is None:
        next_order = max((item.sort_order for item in record.fields), default=-1) + 1
        existing = RecordField(
            record_id=record.id,
            field_key=item_key,
            field_label=normalize_field_label(resolved_label),
            field_type=resolved_type.value,
            sort_order=next_order,
        )
        session.add(existing)

    existing.field_label = normalize_field_label(resolved_label)
    existing.field_type = resolved_type.value
    existing.value_text = encoded.value_text
    existing.value_json = encoded.value_json
    existing.encrypted_value = encoded.encrypted_value
    record.version = (record.version or 1) + 1
    if user_id:
        record.updated_by = user_id

    await session.commit()
    await session.refresh(existing)
    return existing


async def delete_record_field(
    session: AsyncSession,
    record_id: UUID,
    field_id: UUID,
    *,
    user_id: UUID | None = None,
) -> None:
    record = await get_record(session, record_id)
    if record is None:
        raise RecordNotFound("Запись не найдена.")
    field = next((item for item in record.fields if item.id == field_id), None)
    if field is None:
        raise RecordNotFound("Поле записи не найдено.")
    await session.delete(field)
    record.version = (record.version or 1) + 1
    if user_id:
        record.updated_by = user_id
    await session.commit()


async def update_record_tags(
    session: AsyncSession,
    record_id: UUID,
    tags: Sequence[str] | None,
    *,
    user_id: UUID | None = None,
) -> None:
    record = await get_record(session, record_id)
    if record is None:
        raise RecordNotFound("Запись не найдена.")
    await _replace_tags(session, record_id, tags)
    record.version = (record.version or 1) + 1
    if user_id:
        record.updated_by = user_id
    await session.commit()


# ---------------------------------------------------------------------------
# Phase 05: перемещение, окончательное удаление и вложения
# ---------------------------------------------------------------------------


async def move_record(
    session: AsyncSession,
    record_id: UUID,
    new_category_id: UUID,
    user_id: UUID,
    *,
    user: User | None = None,
) -> Record:
    """Перемещает запись в другую категорию с проверкой прав.

    При переданном пользователе действует Default Deny: нужны право EDIT
    на запись и право CREATE в целевой категории. Подкатегория старой
    категории сбрасывается, потому что относится к прежнему разделу.
    """
    record = await get_record(session, record_id)
    if record is None:
        raise RecordNotFound("Запись не найдена.")
    if record.status == "DELETED":
        raise RecordStateError("Удалённую запись нельзя переместить — сначала восстановите её.")

    category = await session.get(Category, new_category_id)
    if category is None or category.status != "ACTIVE":
        raise RecordValidationError("Целевая категория не найдена или недоступна.")

    if user is not None:
        can_edit = await PermissionService.can(
            user, PermissionCode.EDIT, "record", record.id, session
        )
        can_create = await PermissionService.can(
            user, PermissionCode.CREATE, "category", category.id, session
        )
        if not (can_edit and can_create):
            raise RecordPermissionError("Нет прав перемещать запись в эту категорию.")

    if category.id != record.category_id:
        record.category_id = category.id
        record.subcategory_id = None
        record.updated_by = user_id
        await session.commit()
        await session.refresh(record)
    return record


async def permanent_delete(
    session: AsyncSession,
    record_id: UUID,
    user_id: UUID,
    *,
    user: User | None = None,
) -> None:
    """Окончательное (hard) удаление записи со всеми полями, тегами и версиями.

    Удалять можно только скрытые (архивные/удалённые) записи: активные сначала
    архивируют — это защита от случайной потери данных. При переданном
    пользователе проверяется системное право DELETE. Ссылки файлов (record_files)
    снимаются каскадом БД, сами файлы остаются в хранилище для других записей.
    """
    record = (
        await session.execute(
            select(Record)
            .where(Record.id == record_id)
            .options(
                # Все каскадные связи должны быть загружены до удаления —
                # иначе в async-сессии ORM упрётся в lazy load.
                selectinload(Record.fields),
                selectinload(Record.tags),
                selectinload(Record.versions),
                selectinload(Record.file_links),
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise RecordNotFound("Запись не найдена.")
    if user is not None and not await PermissionService.can(
        user, PermissionCode.DELETE, None, None, session
    ):
        raise RecordPermissionError("Нет права окончательно удалять записи.")
    if record.status not in PERMANENTLY_DELETABLE_STATUSES:
        raise RecordStateError(
            "Активную запись нельзя удалить окончательно: сначала архивируйте или удалите её."
        )

    # Избранное ссылается на запись без каскада в БД — чистим вручную.
    # recent_records, record_tags, record_versions, record_permissions
    # и record_files удаляются ON DELETE CASCADE.
    await session.execute(delete(Favorite).where(Favorite.record_id == record.id))
    await session.delete(record)  # ORM-каскад снимает record_fields
    await session.commit()


async def add_attachment(
    session: AsyncSession,
    record_id: UUID,
    file_id: UUID,
) -> RecordFile:
    """Привязывает существующий файл к записи; повторная привязка не дублирует связь."""
    record = await session.get(Record, record_id)
    if record is None:
        raise RecordNotFound("Запись не найдена.")
    file = await session.get(File, file_id)
    if file is None or file.deleted_at is not None:
        raise RecordValidationError("Файл не найден или уже удалён.")

    link = await session.get(RecordFile, {"record_id": record_id, "file_id": file_id})
    if link is not None:
        return link

    count = await session.scalar(
        select(func.count()).select_from(RecordFile).where(RecordFile.record_id == record_id)
    )
    link = RecordFile(record_id=record_id, file_id=file_id, sort_order=count or 0)
    session.add(link)
    await session.commit()
    return link


async def remove_attachment(
    session: AsyncSession,
    record_id: UUID,
    file_id: UUID,
) -> bool:
    """Отвязывает файл от записи. Сам файл остаётся в хранилище (удаляется Phase 06)."""
    result = await session.execute(
        delete(RecordFile).where(
            RecordFile.record_id == record_id,
            RecordFile.file_id == file_id,
        )
    )
    await session.commit()
    return bool(result.rowcount)


async def list_attachments(
    session: AsyncSession,
    record_id: UUID,
) -> list[File]:
    """Вложения записи: существующие (не удалённые) файлы в порядке привязки."""
    result = await session.execute(
        select(File)
        .join(RecordFile, RecordFile.file_id == File.id)
        .where(RecordFile.record_id == record_id, File.deleted_at.is_(None))
        .order_by(RecordFile.sort_order, File.original_name)
    )
    return list(result.scalars().all())


__all__ = [
    "DEFAULT_FIELD_DEFINITIONS",
    "MAX_TITLE_LENGTH",
    "PERMANENTLY_DELETABLE_STATUSES",
    "RESTORABLE_STATUSES",
    "MissingRequiredField",
    "RecordError",
    "RecordNotFound",
    "RecordPermissionError",
    "RecordStateError",
    "RecordValidationError",
    "add_attachment",
    "archive_record",
    "create_record",
    "delete_record_field",
    "get_category_by_code",
    "get_record",
    "list_attachments",
    "list_records",
    "list_records_page",
    "move_record",
    "normalize_access_mode",
    "permanent_delete",
    "remove_attachment",
    "restore_record",
    "set_record_field",
    "soft_delete_record",
    "update_record",
    "update_record_tags",
    "validate_record_description",
    "validate_record_title",
]
