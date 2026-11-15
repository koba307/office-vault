"""Версии записей: снимки состояния, история изменений, восстановление и optimistic locking.

Текущая версия хранится в `records.version`. Строка в `record_versions` — снимок записи,
сделанный сразу после того, как запись получила номер этой версии. Снимок содержит
содержимое записи и поля; секреты остаются в зашифрованном виде и никогда не раскрываются
в истории и сравнениях.
"""

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Record, RecordField, RecordType, RecordVersion

# Поля верхнего уровня, которые попадают в снимок и сравнение версий.
TOP_LEVEL_LABELS: dict[str, str] = {
    "title": "Название",
    "description": "Описание",
    "record_type": "Тип записи",
}


class RecordVersionError(ValueError):
    """Базовая ошибка домена версий записей."""


class RecordNotFound(RecordVersionError):
    """Запись не найдена."""


class VersionNotFound(RecordVersionError):
    """Снимок указанной версии не найден."""


class VersionExists(RecordVersionError):
    """Снимок для текущей версии уже сохранён."""


class VersionConflict(RecordVersionError):
    """Запись изменена другим пользователем с момента открытия (optimistic locking)."""


@dataclass(frozen=True)
class FieldChange:
    """Одно отличие между двумя версиями записи."""

    name: str
    label: str
    old: str | None
    new: str | None
    secret: bool = False


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _field_value(item: dict[str, Any]) -> str | None:
    if item.get("value_text") is not None:
        return item["value_text"]
    if item.get("value_json") is not None:
        return json.dumps(item["value_json"], ensure_ascii=False, sort_keys=True)
    return None


def field_display_text(item: dict[str, Any]) -> str | None:
    """Текст значения поля из снимка. Для секретов всегда None."""
    if item.get("encrypted_value"):
        return None
    return _field_value(item)


def build_snapshot(record: Record) -> dict[str, Any]:
    """Снимок содержимого записи. Секреты сохраняются только в зашифрованном виде."""
    return {
        "title": record.title,
        "description": record.description,
        "record_type": _enum_value(record.record_type),
        "fields": [
            {
                "field_key": field.field_key,
                "field_label": field.field_label,
                "field_type": field.field_type,
                "value_text": field.value_text,
                "value_json": field.value_json,
                "encrypted_value": field.encrypted_value,
                "encryption_key_version": field.encryption_key_version,
                "sort_order": field.sort_order,
            }
            for field in sorted(record.fields, key=lambda f: (f.sort_order, f.field_key))
        ],
    }


def compare_snapshots(old: dict[str, Any], new: dict[str, Any]) -> list[FieldChange]:
    """Сравнивает два снимка. Значения секретов не возвращаются, только факт изменения."""
    changes: list[FieldChange] = []

    for key, label in TOP_LEVEL_LABELS.items():
        before, after = old.get(key), new.get(key)
        if before != after:
            changes.append(FieldChange(key, label, _as_text(before), _as_text(after)))

    old_fields = {item["field_key"]: item for item in old.get("fields", [])}
    new_fields = {item["field_key"]: item for item in new.get("fields", [])}
    for key in sorted(old_fields.keys() | new_fields.keys()):
        before, after = old_fields.get(key), new_fields.get(key)
        sample = after or before or {}
        label = sample.get("field_label", key)
        secret = bool((before or {}).get("encrypted_value") or (after or {}).get("encrypted_value"))

        if secret:
            before_cipher = (before or {}).get("encrypted_value")
            after_cipher = (after or {}).get("encrypted_value")
            if before_cipher != after_cipher:
                changes.append(FieldChange(f"field:{key}", label, None, None, secret=True))
            continue

        before_text = field_display_text(before) if before else None
        after_text = field_display_text(after) if after else None
        if before is None or after is None or before_text != after_text:
            changes.append(
                FieldChange(f"field:{key}", label, before_text, after_text, secret=False)
            )

    return changes


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


async def _load_record(session: AsyncSession, record_id: UUID) -> Record | None:
    # populate_existing нужен, потому что сервис меняет строки через UPDATE/DELETE в той же сессии.
    result = await session.execute(
        select(Record)
        .where(Record.id == record_id)
        .options(selectinload(Record.fields))
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_version(record_id: UUID, version: int, session: AsyncSession) -> RecordVersion | None:
    result = await session.execute(
        select(RecordVersion).where(
            RecordVersion.record_id == record_id, RecordVersion.version == version
        )
    )
    return result.scalar_one_or_none()


def _add_snapshot(
    session: AsyncSession,
    record: Record,
    changed_by: UUID | None,
    change_description: str | None,
) -> RecordVersion:
    row = RecordVersion(
        record_id=record.id,
        version=record.version,
        snapshot=build_snapshot(record),
        changed_by=changed_by,
        change_description=change_description,
    )
    session.add(row)
    return row


async def create_version(
    record_id: UUID,
    changed_by: UUID | None,
    change_description: str | None,
    session: AsyncSession,
) -> RecordVersion:
    """Сохраняет снимок текущего состояния записи под её текущим номером версии."""
    record = await _load_record(session, record_id)
    if record is None:
        raise RecordNotFound("Запись не найдена.")
    if await get_version(record_id, record.version, session) is not None:
        raise VersionExists(f"Версия {record.version} уже сохранена.")

    row = _add_snapshot(session, record, changed_by, change_description)
    await session.commit()
    await session.refresh(row)
    return row


async def list_versions(record_id: UUID, session: AsyncSession) -> list[RecordVersion]:
    """Все сохранённые версии записи, от новых к старым."""
    result = await session.execute(
        select(RecordVersion)
        .where(RecordVersion.record_id == record_id)
        .order_by(RecordVersion.version.desc())
    )
    return list(result.scalars().all())


async def get_version_diff(
    record_id: UUID, v1: int, v2: int, session: AsyncSession
) -> list[FieldChange]:
    """Отличия между версией v1 (было) и версией v2 (стало)."""
    first = await get_version(record_id, v1, session)
    second = await get_version(record_id, v2, session)
    if first is None or second is None:
        missing = v1 if first is None else v2
        raise VersionNotFound(f"Версия {missing} не найдена.")
    return compare_snapshots(first.snapshot or {}, second.snapshot or {})


async def _compare_and_bump(
    session: AsyncSession,
    record_id: UUID,
    expected_version: int,
    values: dict[str, Any],
) -> None:
    """Атомарно меняет запись, только если её версия всё ещё равна expected_version."""
    result = await session.execute(
        update(Record)
        .where(Record.id == record_id, Record.version == expected_version)
        .values(version=expected_version + 1, **values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        return

    exists = await session.scalar(select(Record.id).where(Record.id == record_id))
    if exists is None:
        raise RecordNotFound("Запись не найдена.")
    raise VersionConflict(
        "Запись была изменена другим пользователем. Откройте её заново и повторите действие."
    )


async def update_record_with_lock(
    record_id: UUID,
    expected_version: int,
    session: AsyncSession,
    *,
    changed_by: UUID | None,
    change_description: str | None,
    title: str | None = None,
    description: str | None = None,
) -> Record:
    """Обновляет название/описание с проверкой версии и сохраняет снимок новой версии.

    None означает «не менять поле». Пустая строка в description очищает описание.
    """
    values: dict[str, Any] = {"updated_by": changed_by}
    if title is not None:
        values["title"] = title
    if description is not None:
        values["description"] = description

    await _compare_and_bump(session, record_id, expected_version, values)
    record = await _load_record(session, record_id)
    _add_snapshot(session, record, changed_by, change_description)
    await session.commit()
    await session.refresh(record)
    return record


async def restore_version(
    record_id: UUID,
    version: int,
    session: AsyncSession,
    *,
    changed_by: UUID | None = None,
    expected_version: int | None = None,
) -> Record:
    """Возвращает содержимое записи из снимка версии.

    Восстанавливаются название, описание, тип и поля. Статус, категория и доступ не меняются:
    откат содержимого не должен менять права. Результат — новая версия записи.
    """
    target = await get_version(record_id, version, session)
    if target is None:
        raise VersionNotFound(f"Версия {version} не найдена.")
    snapshot = target.snapshot or {}

    if expected_version is None:
        expected_version = await session.scalar(
            select(Record.version).where(Record.id == record_id)
        )
        if expected_version is None:
            raise RecordNotFound("Запись не найдена.")

    values: dict[str, Any] = {
        "title": snapshot["title"],
        "description": snapshot.get("description"),
        "record_type": RecordType(snapshot["record_type"]),
        "updated_by": changed_by,
    }
    await _compare_and_bump(session, record_id, expected_version, values)

    await session.execute(delete(RecordField).where(RecordField.record_id == record_id))
    for item in snapshot.get("fields", []):
        session.add(
            RecordField(
                record_id=record_id,
                field_key=item["field_key"],
                field_label=item["field_label"],
                field_type=item["field_type"],
                value_text=item.get("value_text"),
                value_json=item.get("value_json"),
                encrypted_value=item.get("encrypted_value"),
                encryption_key_version=item.get("encryption_key_version"),
                sort_order=item.get("sort_order", 0),
            )
        )
    await session.flush()

    record = await _load_record(session, record_id)
    _add_snapshot(session, record, changed_by, f"Восстановлено из версии {version}")
    await session.commit()
    await session.refresh(record)
    return record
