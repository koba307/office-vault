"""Сборка табличного представления записей для экспорта (без I/O и без БД).

Колонки бывают двух видов: базовые (id, категория, тип, название и т.д.) и динамические —
ключи полей записи (например ``login``, ``password``). Секретные поля (PASSWORD/SECRET)
в открытом виде попадают в файл только при явно подтверждённом ``include_secrets``;
иначе выводится маска ``***``, совместимая с импортом (он пропускает такие значения).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.core.encryption import decrypt_secret
from app.database.models import FieldType, Record

BASE_COLUMNS: tuple[str, ...] = (
    "id",
    "category",
    "type",
    "title",
    "description",
    "status",
    "tags",
    "created_at",
    "updated_at",
)
ATTACHMENTS_COLUMN = "attachments"
SECRET_EXPORT_MASK = "***"
SECRET_TYPES = frozenset({FieldType.PASSWORD.value, FieldType.SECRET.value})


class ColumnSelectionError(ValueError):
    """Запрошены колонки, которых нет в выборке."""


@dataclass(frozen=True)
class ExportTable:
    """Плоская таблица: порядок колонок и строки-словари."""

    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def dynamic_field_keys(records: Iterable[Record]) -> list[str]:
    """Все ключи динамических полей выборки в детерминированном порядке."""
    keys: set[str] = set()
    for record in records:
        for item in record.fields or []:
            keys.add(item.field_key)
    return sorted(keys)


def available_columns(
    records: Sequence[Record],
    *,
    with_attachments: bool,
) -> tuple[str, ...]:
    """Все колонки, которые можно выбрать для выборки: базовые, динамические и вложения."""
    columns = list(BASE_COLUMNS) + dynamic_field_keys(records)
    if with_attachments:
        columns.append(ATTACHMENTS_COLUMN)
    return tuple(columns)


def resolve_columns(
    records: Sequence[Record],
    fields: Sequence[str] | None,
    *,
    with_attachments: bool,
) -> tuple[str, ...]:
    """Итоговый порядок колонок. ``None`` — все доступные; неизвестное имя — ошибка."""
    available = available_columns(records, with_attachments=with_attachments)
    if fields is None:
        return available
    requested = [str(name).strip() for name in fields if str(name).strip()]
    unknown = [name for name in requested if name not in available]
    if unknown:
        raise ColumnSelectionError(f"Неизвестные поля экспорта: {', '.join(unknown)}.")
    if not requested:
        raise ColumnSelectionError("Выберите хотя бы одно поле для экспорта.")
    ordered: list[str] = []
    for name in requested:
        if name not in ordered:
            ordered.append(name)
    # Вложения задаются флагом выгрузки, а не списком полей: колонка есть, если они включены.
    if with_attachments and ATTACHMENTS_COLUMN not in ordered:
        ordered.append(ATTACHMENTS_COLUMN)
    return tuple(ordered)


def _field_value(record: Record, key: str, *, include_secrets: bool) -> str:
    for item in record.fields or []:
        if item.field_key != key:
            continue
        if item.field_type in SECRET_TYPES or item.encrypted_value:
            if not include_secrets:
                return SECRET_EXPORT_MASK
            if item.encrypted_value:
                return decrypt_secret(item.encrypted_value)
        return item.value_text or ""
    return ""


def _base_value(
    record: Record,
    column: str,
    *,
    attachments: Mapping[UUID, Sequence[str]] | None,
) -> Any:
    if column == "id":
        return str(record.id)
    if column == "category":
        return record.category.code if record.category else ""
    if column == "type":
        return record.record_type.value if record.record_type else ""
    if column == "title":
        return record.title or ""
    if column == "description":
        return record.description or ""
    if column == "status":
        return record.status or ""
    if column == "tags":
        names = sorted(link.tag.name for link in record.tags or [] if link.tag)
        return ", ".join(names)
    if column == "created_at":
        return record.created_at.isoformat() if record.created_at else ""
    if column == "updated_at":
        return record.updated_at.isoformat() if record.updated_at else ""
    if column == ATTACHMENTS_COLUMN:
        return ", ".join((attachments or {}).get(record.id, ()))
    return ""


def build_table(
    records: Sequence[Record],
    *,
    fields: Sequence[str] | None = None,
    include_secrets: bool = False,
    attachments: Mapping[UUID, Sequence[str]] | None = None,
) -> ExportTable:
    """Собирает таблицу. ``attachments`` — имена файлов по записям; ``None`` — колонки нет."""
    columns = resolve_columns(records, fields, with_attachments=attachments is not None)
    base_set = set(BASE_COLUMNS) | {ATTACHMENTS_COLUMN}
    rows = []
    for record in records:
        row: dict[str, Any] = {}
        for column in columns:
            if column in base_set:
                row[column] = _base_value(record, column, attachments=attachments)
            else:
                row[column] = _field_value(record, column, include_secrets=include_secrets)
        rows.append(row)
    return ExportTable(columns=columns, rows=tuple(rows))
