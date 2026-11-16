"""Экспорт записей в CSV, XLSX, JSON и ZIP с проверкой прав и аудитом (Phase 08).

Правила безопасности:

- Экспорт требует право EXPORT на категорию (или системное, если категория не выбрана).
- Каждая запись попадает в выгрузку только при праве VIEW на неё (Default Deny).
- Секреты (PASSWORD/SECRET) выгружаются в открытом виде только при двух условиях:
  роль SUPERADMIN или BOSS и право EXPORT_SECRETS. Иначе выводится маска.
- Каждая выгрузка пишется в журнал аудита; выгрузка секретов — отдельным событием
  EXPORT_SECRETS со списком ID записей. Отказы тоже пишутся в журнал.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    Category,
    File,
    PermissionCode,
    Record,
    RecordFile,
    RecordTag,
    RecordType,
    RoleCode,
    User,
)
from app.services.auth.permissions import log_audit
from app.services.import_export.export_formats import (
    EXPORT_FORMATS,
    FORMAT_EXTENSIONS,
    FORMAT_MIME_TYPES,
    csv_bytes,
    json_bytes,
    xlsx_bytes,
)
from app.services.import_export.rows import ColumnSelectionError, available_columns, build_table
from app.services.import_export.zip_export import build_zip_export
from app.services.permissions.context import load_access_context
from app.services.permissions.errors import PermissionDenied
from app.services.permissions.record import record_denial_reason
from app.services.permissions.service import (
    DENY_MESSAGES,
    TARGET_CATEGORY,
    PermissionService,
)
from app.services.records.fields import FieldValidationError, normalize_record_type

EXPORT_MAX_RECORDS = 5000
SECRET_EXPORT_ROLES = frozenset({RoleCode.SUPERADMIN, RoleCode.BOSS})
_ID_SPLIT_RE = re.compile(r"[\s,;]+")


class ExportError(ValueError):
    """Базовая ошибка экспорта."""


class ExportValidationError(ExportError):
    """Некорректные параметры экспорта."""


class ExportPermissionError(ExportError):
    """Недостаточно прав для экспорта."""


class ExportLimitError(ExportError):
    """Выборка больше допустимого размера."""


@dataclass(frozen=True)
class ExportResult:
    """Готовый файл экспорта и итоги для отчёта."""

    data: bytes
    filename: str
    mime_type: str
    format: str
    record_count: int
    file_count: int
    include_secrets: bool
    columns: tuple[str, ...] = ()
    skipped_files: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def normalize_format(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in EXPORT_FORMATS:
        raise ExportValidationError(f"Неподдерживаемый формат экспорта: {value}.")
    return normalized


def secret_export_role_allowed(role: RoleCode | str | None) -> bool:
    """Первая линия защиты: только руководитель и администратор. Неизвестная роль — отказ."""
    if isinstance(role, RoleCode):
        return role in SECRET_EXPORT_ROLES
    try:
        return RoleCode(str(role).strip().upper()) in SECRET_EXPORT_ROLES
    except ValueError:
        return False


def build_export_filename(fmt: str, *, moment: datetime, include_secrets: bool) -> str:
    """Имя файла формируется только из служебных данных; ввод пользователя в имя не попадает."""
    prefix = "office_vault_secrets_export" if include_secrets else "office_vault_export"
    return f"{prefix}_{moment:%Y%m%d_%H%M%S}.{FORMAT_EXTENSIONS[fmt]}"


def parse_record_ids(raw: Sequence[Any] | None) -> list[UUID] | None:
    """Приводит ID записей к UUID без дублей. ``None`` означает «все записи»."""
    if raw is None:
        return None
    ids: list[UUID] = []
    for item in raw:
        try:
            value = item if isinstance(item, UUID) else UUID(str(item).strip())
        except ValueError as exc:
            raise ExportValidationError(f"Некорректный ID записи: {item}.") from exc
        if value not in ids:
            ids.append(value)
    if len(ids) > EXPORT_MAX_RECORDS:
        raise ExportLimitError(f"Нельзя выгрузить больше {EXPORT_MAX_RECORDS} записей за раз.")
    return ids


def parse_record_ids_text(text: str) -> list[UUID]:
    """ID, введённые пользователем через пробел, запятую или точку с запятой."""
    parts = [part for part in _ID_SPLIT_RE.split(str(text or "").strip()) if part]
    if not parts:
        raise ExportValidationError("Укажите хотя бы один ID записи.")
    return parse_record_ids(parts) or []


def normalize_record_type_filter(value: RecordType | str | None) -> RecordType | None:
    if value is None or value == "":
        return None
    try:
        return normalize_record_type(value)
    except FieldValidationError as exc:
        raise ExportValidationError(str(exc)) from exc


async def resolve_category_code(session: AsyncSession, code: str | None) -> Category | None:
    if not code:
        return None
    category = (
        await session.execute(select(Category).where(Category.code == code))
    ).scalar_one_or_none()
    if category is None:
        raise ExportValidationError("Категория не найдена.")
    return category


async def ensure_export_rights(
    session: AsyncSession,
    user: User | None,
    *,
    category: Category | None,
    include_secrets: bool,
) -> None:
    """Проверяет право на выгрузку. Проверка роли идёт раньше обращения к БД."""
    if user is None:
        raise ExportPermissionError("Пользователь не найден.")
    if include_secrets and not secret_export_role_allowed(user.role):
        raise ExportPermissionError(
            "Экспорт секретов доступен только руководителю и администратору."
        )
    try:
        if category is not None:
            await PermissionService.require(
                user, PermissionCode.EXPORT, TARGET_CATEGORY, category.id, session
            )
        else:
            await PermissionService.require(user, PermissionCode.EXPORT, None, None, session)
        if include_secrets:
            await PermissionService.require(
                user, PermissionCode.EXPORT_SECRETS, None, None, session
            )
    except PermissionDenied as exc:
        raise ExportPermissionError(DENY_MESSAGES.get(exc.reason, "Нет доступа.")) from exc


async def can_export_secrets(session: AsyncSession, user: User | None) -> bool:
    """Можно ли пользователю выгружать секреты (роль и право EXPORT_SECRETS)."""
    if user is None or not secret_export_role_allowed(user.role):
        return False
    return await PermissionService.can(user, PermissionCode.EXPORT_SECRETS, None, None, session)


async def select_export_records(
    session: AsyncSession,
    user: User,
    *,
    category: Category | None,
    record_type: RecordType | None,
    record_ids: Sequence[UUID] | None,
) -> list[Record]:
    """Активные записи выборки, которые пользователь вправе просматривать.

    Записи, которых нет в выборке или к которым нет доступа, молча исключаются:
    ответ не раскрывает существование чужих записей.
    """
    if record_ids is not None and not record_ids:
        return []
    conditions = [Record.status == "ACTIVE"]
    if category is not None:
        conditions.append(Record.category_id == category.id)
    if record_type is not None:
        conditions.append(Record.record_type == record_type)
    if record_ids is not None:
        conditions.append(Record.id.in_(list(record_ids)))

    result = await session.execute(
        select(Record)
        .where(*conditions)
        .options(
            selectinload(Record.fields),
            selectinload(Record.category),
            selectinload(Record.tags).selectinload(RecordTag.tag),
        )
        .order_by(Record.created_at, Record.id)
    )
    candidates = list(result.scalars().all())
    if len(candidates) > EXPORT_MAX_RECORDS:
        raise ExportLimitError(
            f"В выборке больше {EXPORT_MAX_RECORDS} записей. Сузьте её категорией или типом."
        )

    ctx, reason = await load_access_context(user, session)
    if reason is not None or ctx is None:
        raise ExportPermissionError("Нет доступа к экспорту.")
    visible: list[Record] = []
    for record in candidates:
        if await record_denial_reason(ctx, record.id, PermissionCode.VIEW, session) is None:
            visible.append(record)
    return visible


async def _attachment_names(
    session: AsyncSession, record_ids: Sequence[UUID]
) -> dict[UUID, list[str]]:
    if not record_ids:
        return {}
    result = await session.execute(
        select(RecordFile.record_id, File.original_name)
        .join(File, File.id == RecordFile.file_id)
        .where(RecordFile.record_id.in_(list(record_ids)), File.deleted_at.is_(None))
        .order_by(RecordFile.record_id, RecordFile.sort_order, File.original_name)
    )
    names: dict[UUID, list[str]] = {}
    for record_id, name in result.all():
        names.setdefault(record_id, []).append(name)
    return names


async def available_export_columns(
    session: AsyncSession,
    user: User,
    *,
    category_code: str | None,
    record_type: RecordType | str | None,
    record_ids: Sequence[UUID] | None,
    with_attachments: bool,
) -> list[str]:
    """Колонки, которые можно выбрать для выборки (для шага выбора полей в боте)."""
    category = await resolve_category_code(session, category_code)
    await ensure_export_rights(session, user, category=category, include_secrets=False)
    records = await select_export_records(
        session,
        user,
        category=category,
        record_type=normalize_record_type_filter(record_type),
        record_ids=record_ids,
    )
    return list(available_columns(records, with_attachments=with_attachments))


async def _audit_denied(
    session: AsyncSession, user: User | None, *, include_secrets: bool, fmt: str
) -> None:
    if user is None:
        return
    await log_audit(
        session,
        user.id,
        "EXPORT_SECRETS" if include_secrets else "EXPORT_RECORDS",
        "Record",
        None,
        result="DENIED",
        metadata={"format": fmt},
    )


async def export_records(
    session: AsyncSession,
    user: User,
    *,
    category_code: str | None = None,
    record_type: RecordType | str | None = None,
    record_ids: Sequence[UUID | str] | None = None,
    fields: Sequence[str] | None = None,
    format: str = "csv",
    include_files: bool = False,
    include_secrets: bool = False,
) -> ExportResult:
    """Выгрузка записей в выбранный формат.

    ``fields`` — подмножество колонок (``None`` — все). ``include_files`` добавляет вложения:
    для ZIP — сами файлы, для остальных форматов — их имена. ``include_secrets`` выводит
    секреты в открытом виде и доступен только при праве EXPORT_SECRETS.
    """
    fmt = normalize_format(format)
    category = await resolve_category_code(session, category_code)
    rtype = normalize_record_type_filter(record_type)
    ids = parse_record_ids(record_ids)

    try:
        await ensure_export_rights(
            session, user, category=category, include_secrets=include_secrets
        )
    except ExportPermissionError:
        await _audit_denied(session, user, include_secrets=include_secrets, fmt=fmt)
        raise

    records = await select_export_records(
        session, user, category=category, record_type=rtype, record_ids=ids
    )
    moment = datetime.now(UTC)
    columns: tuple[str, ...] = ()
    file_count = 0
    skipped: tuple[dict[str, Any], ...] = ()
    try:
        if fmt == "zip":
            archive = await build_zip_export(
                session,
                user,
                records,
                include_files,
                fields=fields,
                include_secrets=include_secrets,
                generated_at=moment,
            )
            data = archive.data
            file_count = archive.file_count
            skipped = archive.skipped_files
        else:
            attachments = (
                await _attachment_names(session, [record.id for record in records])
                if include_files
                else None
            )
            table = build_table(
                records,
                fields=fields,
                include_secrets=include_secrets,
                attachments=attachments,
            )
            columns = table.columns
            if fmt == "csv":
                data = csv_bytes(table)
            elif fmt == "xlsx":
                data = xlsx_bytes(table)
            else:
                data = json_bytes(table, generated_at=moment)
    except ColumnSelectionError as exc:
        raise ExportValidationError(str(exc)) from exc

    await log_audit(
        session,
        user.id,
        "EXPORT_RECORDS",
        "Record",
        None,
        metadata={
            "format": fmt,
            "category": category_code,
            "record_type": rtype.value if rtype else None,
            "record_count": len(records),
            "file_count": file_count,
            "include_files": include_files,
            "include_secrets": include_secrets,
        },
    )
    if include_secrets:
        await log_audit(
            session,
            user.id,
            "EXPORT_SECRETS",
            "Record",
            None,
            metadata={
                "format": fmt,
                "record_count": len(records),
                "record_ids": [str(record.id) for record in records],
            },
        )

    return ExportResult(
        data=data,
        filename=build_export_filename(fmt, moment=moment, include_secrets=include_secrets),
        mime_type=FORMAT_MIME_TYPES[fmt],
        format=fmt,
        record_count=len(records),
        file_count=file_count,
        include_secrets=include_secrets,
        columns=columns,
        skipped_files=skipped,
    )
