"""Импорт/экспорт записей: CSV-совместимость и фоновые задачи импорта.

Старый ``import_records_csv`` оставлен для обратной совместимости с ранними
хендлерами. Полный сценарий Phase 08 идёт через :func:`create_import_job` и
:func:`import_records`: задача хранит прогресс, ошибки по строкам и отчёт,
а отмена прекращает импорт до обработки следующей строки.
"""

import csv
import io
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import (
    Category,
    ImportError,
    ImportJob,
    JobStatus,
    Record,
    RecordType,
    User,
)
from app.services.import_export.formats import parse_file
from app.services.import_export.preview import RESERVED_COLUMNS, RowError, apply_field_mapping
from app.services.records.service import create_record, get_category_by_code

MAX_STORED_ERRORS = 200
MAX_REPORT_ERRORS = 50
CANCEL_CHECK_EVERY = 25

ProgressCallback = Callable[[int, int], Awaitable[None] | None]


class ImportJobError(ValueError):
    """Базовая ошибка задачи импорта."""


class ImportJobNotFound(ImportJobError):
    """Задача импорта не найдена."""


class ImportCancelled(ImportJobError):
    """Задача отменена до завершения импорта."""


@dataclass(frozen=True)
class ImportStatus:
    """Снимок состояния задачи для бота и API."""

    job_id: UUID
    status: JobStatus
    total_rows: int
    success_rows: int
    failed_rows: int
    created_at: datetime | None = None
    finished_at: datetime | None = None
    report: dict[str, Any] | None = None

    @property
    def processed_rows(self) -> int:
        return self.success_rows + self.failed_rows

    @property
    def percent(self) -> int:
        if self.status == JobStatus.COMPLETED:
            return 100
        if self.total_rows <= 0:
            return 0
        return min(100, int(self.processed_rows * 100 / self.total_rows))


@dataclass
class ImportResult:
    """Итог фонового импорта."""

    job_id: UUID | None
    status: JobStatus
    total_rows: int
    success_rows: int
    failed_rows: int = 0
    skipped_rows: int = 0
    errors: list[RowError] = field(default_factory=list)
    cancelled: bool = False


_FINISHED_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
_SECRET_MARKERS = ("password", "secret", "token", "api_key", "пароль", "секрет")


# ---------------------------------------------------------------------------
# Совместимость: простой CSV-импорт без задачи
# ---------------------------------------------------------------------------


async def import_records_csv(
    session: AsyncSession,
    *,
    category_code: str,
    csv_text: str,
    created_by: User,
) -> int:
    reader = csv.DictReader(io.StringIO(csv_text))
    count = 0
    for row in reader:
        title = row.get("title") or "Без названия"
        rtype_raw = (row.get("type") or "OTHER").upper()
        try:
            rtype = RecordType(rtype_raw)
        except ValueError:
            rtype = RecordType.OTHER
        description = row.get("description") or None

        fields = {}
        secret_fields = set()
        fields_raw = row.get("fields") or ""
        for part in fields_raw.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            value = value.strip()
            if value == "***":
                continue
            fields[key] = value
            if "password" in key.lower() or "secret" in key.lower():
                secret_fields.add(key)

        await create_record(
            session,
            category_code=category_code,
            record_type=rtype,
            title=title,
            description=description,
            created_by=created_by,
            fields=fields,
            secret_fields=secret_fields,
        )
        count += 1
    return count


async def export_records_csv(session: AsyncSession, category_code: str | None = None) -> str:
    q = select(Record).where(Record.status == "ACTIVE").options(selectinload(Record.fields))
    if category_code:
        cat = await get_category_by_code(session, category_code)
        if cat:
            q = q.where(Record.category_id == cat.id)
    result = await session.execute(q)
    records = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "title", "type", "description", "fields"])
    for r in records:
        fields_str = "; ".join(f"{f.field_key}={f.value_text or '***'}" for f in r.fields)
        writer.writerow([str(r.id), r.title, r.record_type.value, r.description or "", fields_str])
    return output.getvalue()


# ---------------------------------------------------------------------------
# Задачи импорта
# ---------------------------------------------------------------------------


async def create_import_job(
    session: AsyncSession,
    user: User,
    category_code: str,
    file_id: UUID | None = None,
    *,
    source_format: str | None = None,
    total_rows: int = 0,
) -> ImportJob:
    """Создаёт задачу импорта в статусе PENDING."""
    if user is None or getattr(user, "id", None) is None:
        raise ImportJobError("Для импорта нужен зарегистрированный пользователь.")
    category = await get_category_by_code(session, category_code)
    if category is None:
        raise ImportJobError(f"Категория {category_code} не найдена.")
    if getattr(category, "status", "ACTIVE") != "ACTIVE":
        raise ImportJobError(f"Категория {category_code} архивирована.")

    job = ImportJob(
        user_id=user.id,
        category_id=category.id,
        file_id=file_id,
        status=JobStatus.PENDING,
        source_format=(source_format or None),
        total_rows=max(0, int(total_rows)),
    )
    session.add(job)
    await session.commit()
    return job


async def get_import_job(session: AsyncSession, job_id: UUID | str) -> ImportJob | None:
    try:
        identifier = UUID(str(job_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return await session.get(ImportJob, identifier)


async def get_import_status(session: AsyncSession, job_id: UUID | str) -> ImportStatus | None:
    """Возвращает прогресс задачи или ``None``, если задачи нет."""
    job = await get_import_job(session, job_id)
    if job is None:
        return None
    return ImportStatus(
        job_id=job.id,
        status=job.status,
        total_rows=job.total_rows,
        success_rows=job.success_rows,
        failed_rows=job.failed_rows,
        created_at=job.created_at,
        finished_at=job.finished_at,
        report=job.report,
    )


async def cancel_import(session: AsyncSession, job_id: UUID | str) -> bool:
    """Отменяет задачу, если она ещё не завершена. Возвращает факт отмены."""
    job = await get_import_job(session, job_id)
    if job is None:
        raise ImportJobNotFound("Задача импорта не найдена.")
    if job.status in _FINISHED_STATUSES:
        return False
    report = dict(job.report or {})
    report.update({"cancelled": True, "cancelled_at": datetime.now(UTC).isoformat()})
    await session.execute(
        update(ImportJob)
        .where(ImportJob.id == job.id)
        .values(status=JobStatus.CANCELLED, finished_at=datetime.now(UTC), report=report)
    )
    await session.commit()
    return True


# ---------------------------------------------------------------------------
# Разбор строки импорта
# ---------------------------------------------------------------------------


def _split_row(
    row: Mapping[str, Any],
) -> tuple[str, RecordType, str | None, dict[str, str], list[str]]:
    """Делит строку на название, тип, описание, динамические поля и теги."""
    title = str(row.get("title") or "").strip()
    raw_type = str(row.get("type") or "").strip()
    try:
        record_type = RecordType(raw_type.upper()) if raw_type else RecordType.OTHER
    except ValueError:
        record_type = RecordType.OTHER
    description = str(row.get("description") or "").strip() or None
    tags = [item.strip() for item in re.split(r"[,;]", str(row.get("tags") or "")) if item.strip()]
    fields: dict[str, str] = {}
    for key, value in row.items():
        if key in RESERVED_COLUMNS:
            continue
        text = str(value or "").strip()
        if text:
            fields[str(key)] = text
    return title, record_type, description, fields, tags


def _secret_field_keys(fields: Mapping[str, str]) -> set[str]:
    return {key for key in fields if any(marker in key.lower() for marker in _SECRET_MARKERS)}


async def _notify_progress(progress: ProgressCallback | None, done: int, total: int) -> None:
    if progress is None:
        return
    outcome = progress(done, total)
    if outcome is not None:
        await outcome


async def _load_rows(
    file_bytes: bytes | None,
    rows: list[dict[str, str]] | None,
    filename: str | None,
    encoding: str | None,
) -> tuple[list[dict[str, str]], str | None]:
    if rows is not None:
        return rows, None
    if not file_bytes:
        raise ImportJobError("Не переданы ни строки, ни файл для импорта.")
    table = parse_file(file_bytes, filename, encoding=encoding)
    return table.rows, table.format


async def import_records(
    session: AsyncSession,
    job_id: UUID | str,
    mapping: dict[str, str] | None = None,
    *,
    rows: list[dict[str, str]] | None = None,
    file_bytes: bytes | None = None,
    filename: str | None = None,
    encoding: str | None = None,
    progress: ProgressCallback | None = None,
    check_duplicates: bool = True,
) -> ImportResult:
    """Импортирует записи в рамках задачи, обновляя прогресс и отчёт.

    Строки можно передать готовыми (``rows``) или дать байты файла
    (``file_bytes`` + ``filename``). ``mapping`` — словарь «колонка → поле»;
    пустое сопоставление означает, что строки уже в ключах полей.
    """
    job = await get_import_job(session, job_id)
    if job is None:
        raise ImportJobNotFound("Задача импорта не найдена.")
    if job.status == JobStatus.CANCELLED:
        raise ImportCancelled("Задача импорта отменена.")
    if job.status in _FINISHED_STATUSES:
        raise ImportJobError("Задача импорта уже завершена.")

    identifier = job.id
    user_id = job.user_id
    category_id = job.category_id
    source_format = job.source_format

    raw_rows, detected_format = await _load_rows(file_bytes, rows, filename, encoding)
    mapped_rows = apply_field_mapping(raw_rows, mapping) if mapping else list(raw_rows)
    total = len(mapped_rows)

    user = await session.get(User, user_id)
    if user is None:
        raise ImportJobError("Пользователь задачи импорта не найден.")
    category = await session.get(Category, category_id) if category_id else None
    if category is None:
        raise ImportJobError("Категория задачи импорта не найдена.")
    category_code = category.code
    source_format = source_format or detected_format

    existing_titles: set[str] = set()
    if check_duplicates:
        result = await session.execute(
            select(Record.title).where(
                Record.category_id == category_id,
                Record.status == "ACTIVE",
            )
        )
        existing_titles = {str(title).casefold() for title in result.scalars().all()}

    await session.execute(
        update(ImportJob)
        .where(ImportJob.id == identifier)
        .values(
            status=JobStatus.PROCESSING,
            total_rows=total,
            success_rows=0,
            failed_rows=0,
            source_format=source_format,
        )
    )
    await session.commit()
    await _notify_progress(progress, 0, total)

    success = 0
    failed = 0
    skipped = 0
    cancelled = False
    errors: list[RowError] = []
    db_errors: list[ImportError] = []
    stored_errors = 0
    seen_titles: set[str] = set()
    cancel_check_every = max(CANCEL_CHECK_EVERY, total // 50 or 1)

    for index, row in enumerate(mapped_rows, start=1):
        if index % cancel_check_every == 1:
            current_status = await session.scalar(
                select(ImportJob.status).where(ImportJob.id == identifier)
            )
            if current_status == JobStatus.CANCELLED:
                cancelled = True
                break

        title, record_type, description, fields, tags = _split_row(row)
        if not title:
            title = f"Строка {index}"
        title_key = title.casefold()
        if check_duplicates and (title_key in existing_titles or title_key in seen_titles):
            skipped += 1
            errors.append(RowError(index, "title", "Дубликат названия — строка пропущена."))
            continue
        seen_titles.add(title_key)

        try:
            # Savepoint: сбойная строка не откатывает уже подготовленные записи.
            async with session.begin_nested():
                await create_record(
                    session,
                    category_code=category_code,
                    record_type=record_type,
                    title=title,
                    description=description,
                    created_by=user,
                    fields=fields,
                    secret_fields=_secret_field_keys(fields),
                    tags=tags or None,
                    commit=False,
                )
            success += 1
        except Exception as exc:  # noqa: BLE001 - ошибка строки не должна ронять импорт
            failed += 1
            message = str(exc) or exc.__class__.__name__
            errors.append(RowError(index, None, message))
            if stored_errors < MAX_STORED_ERRORS:
                db_errors.append(
                    ImportError(
                        job_id=identifier,
                        row_number=index,
                        error=message[:2000],
                        raw_data={key: str(value)[:500] for key, value in row.items()},
                    )
                )
                stored_errors += 1

        if index % 10 == 0 or index == total:
            await _notify_progress(progress, index, total)

    # Отмена до фиксации: ни одна запись не должна попасть в БД.
    discarded = 0
    if cancelled:
        discarded = success
        success = 0
        with suppress(Exception):
            await session.rollback()

    if db_errors:
        session.add_all(db_errors)
    finished_at = datetime.now(UTC)
    if not cancelled:
        final_status = await session.scalar(
            select(ImportJob.status).where(ImportJob.id == identifier)
        )
        cancelled = final_status == JobStatus.CANCELLED
    if cancelled:
        discarded = success
        success = 0
        status = JobStatus.CANCELLED
    else:
        discarded = 0
        status = JobStatus.COMPLETED
    report = {
        "format": source_format,
        "total_rows": total,
        "success_rows": success,
        "failed_rows": failed,
        "skipped_rows": skipped,
        "discarded_rows": discarded,
        "errors": [error.as_dict() for error in errors[:MAX_REPORT_ERRORS]],
        "errors_truncated": len(errors) > MAX_REPORT_ERRORS,
        "cancelled": cancelled,
        "finished_at": finished_at.isoformat(),
    }
    try:
        await session.execute(
            update(ImportJob)
            .where(ImportJob.id == identifier)
            .values(
                status=status,
                total_rows=total,
                success_rows=success,
                failed_rows=failed,
                report=report,
                finished_at=finished_at,
            )
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - финализация не должна падать без отчёта
        with suppress(Exception):
            await session.rollback()
        failure_report = {
            "format": source_format,
            "total_rows": total,
            "success_rows": success,
            "failed_rows": failed,
            "skipped_rows": skipped,
            "discarded_rows": discarded,
            "errors": [error.as_dict() for error in errors[:MAX_REPORT_ERRORS]],
            "errors_truncated": len(errors) > MAX_REPORT_ERRORS,
            "cancelled": cancelled,
            "error": str(exc) or exc.__class__.__name__,
            "finished_at": finished_at.isoformat(),
        }
        with suppress(Exception):
            await session.execute(
                update(ImportJob)
                .where(ImportJob.id == identifier)
                .values(
                    status=JobStatus.FAILED,
                    total_rows=total,
                    success_rows=success,
                    failed_rows=failed,
                    report=failure_report,
                    finished_at=finished_at,
                )
            )
            await session.commit()
        await _notify_progress(progress, total, total)
        return ImportResult(
            job_id=identifier,
            status=JobStatus.FAILED,
            total_rows=total,
            success_rows=success,
            failed_rows=failed,
            skipped_rows=skipped,
            errors=errors,
            cancelled=cancelled,
        )

    await _notify_progress(progress, total, total)

    return ImportResult(
        job_id=identifier,
        status=status,
        total_rows=total,
        success_rows=success,
        failed_rows=failed,
        skipped_rows=skipped,
        errors=errors,
        cancelled=cancelled,
    )
