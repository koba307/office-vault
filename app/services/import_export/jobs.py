"""Жизненный цикл фоновых задач импорта/экспорта (Phase 08).

Модуль инкапсулирует работу с таблицами ``import_jobs``/``export_jobs``:
создание задач, атомарный захват задачи воркером (защита от повторного
выполнения), запрос отмены до фиксации (флаг ``cancel_requested`` ставится
серверной стороной через ``jsonb_set``, чтобы не затереть конкурентные
обновления прогресса), обновление счётчиков и финализация.

Файл-источник импорта и артефакт экспорта хранятся в S3-совместимом
хранилище через примитивы сервиса файлов (``store_object``): реестр
``files`` остаётся единой точкой учёта объектов.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import bindparam, func, literal_column, select, true, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Category,
    ExportJob,
    File,
    ImportError,
    ImportJob,
    JobStatus,
    ScanStatus,
    User,
)
from app.services.files.metadata import calculate_sha256, sanitize_filename
from app.services.files.quarantine import build_storage_key
from app.services.files.service import store_object

CANCEL_REQUEST_KEY = "cancel_requested"

_EMPTY_JSONB = literal_column("'{}'::jsonb")
_CANCEL_PATH = literal_column("'{cancel_requested}'::text[]")

# Форматы-источники фонового импорта (текстовые форматы Phase 08).
IMPORT_FORMAT_BY_EXTENSION = {
    "csv": "CSV",
    "tsv": "TSV",
    "txt": "TXT",
    "json": "JSON",
    "jsonl": "JSONL",
}


def detect_import_format(filename: str | None) -> str | None:
    """Формат импорта по расширению файла; None — формат не распознан."""
    name = sanitize_filename(filename) or ""
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return IMPORT_FORMAT_BY_EXTENSION.get(extension)


async def store_import_source(
    session: AsyncSession,
    *,
    user: User,
    data: bytes,
    filename: str,
    content_type: str | None = None,
) -> File:
    """Кладёт файл-источник импорта в S3 и регистрирует его в ``files``.

    Файл источника — технический вход задачи, а не рабочий документ:
    антивирусная проверка для него не запускается (scan_status=SKIPPED),
    байты читает только воркер импорта.
    """
    payload = bytes(data or b"")
    clean_name = sanitize_filename(filename) or "import.txt"
    storage_key = build_storage_key("imports", clean_name)
    bucket = store_object(payload, storage_key, content_type or "text/plain")
    file = File(
        original_name=clean_name,
        storage_key=storage_key,
        bucket=bucket,
        mime_type=(content_type or "text/plain").split(";", 1)[0],
        extension=clean_name.rsplit(".", 1)[-1].lower() if "." in clean_name else None,
        size=len(payload),
        sha256=calculate_sha256(payload),
        scan_status=ScanStatus.SKIPPED,
        uploaded_by=user.id,
    )
    session.add(file)
    await session.commit()
    return file


async def create_import_job(
    session: AsyncSession,
    *,
    user: User,
    category: Category | None,
    source_file: File | None,
    source_format: str | None,
) -> ImportJob:
    """Создаёт PENDING-задачу импорта (фиксация входа: файл, категория, формат)."""
    job = ImportJob(
        user_id=user.id,
        category_id=category.id if category else None,
        status=JobStatus.PENDING,
        source_format=source_format,
        file_id=source_file.id if source_file else None,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def create_export_job(
    session: AsyncSession,
    *,
    user: User,
    export_format: str,
    category: Category | None = None,
    filters: dict | None = None,
) -> ExportJob:
    """Создаёт PENDING-задачу экспорта."""
    job = ExportJob(
        user_id=user.id,
        category_id=category.id if category else None,
        status=JobStatus.PENDING,
        format=export_format,
        filters=filters or {},
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def claim_import_job(session: AsyncSession, job_id: UUID) -> ImportJob | None:
    """Атомарный захват PENDING-задачи импорта (идемпотентность воркера).

    Возвращает None, если задача уже обрабатывается или завершена:
    повторный запуск воркера в этом случае ничего не делает.
    """
    return await _claim_job(session, ImportJob, job_id)


async def claim_export_job(session: AsyncSession, job_id: UUID) -> ExportJob | None:
    """Атомарный захват PENDING-задачи экспорта."""
    return await _claim_job(session, ExportJob, job_id)


async def _claim_job(session: AsyncSession, model, job_id: UUID):
    result = await session.execute(
        update(model)
        .where(model.id == job_id, model.status == JobStatus.PENDING)
        .values(status=JobStatus.PROCESSING)
    )
    await session.commit()
    if result.rowcount != 1:
        return None
    return await session.get(model, job_id)


async def request_import_cancel(session: AsyncSession, job_id: UUID) -> bool:
    """Запрос отмены импорта до фиксации; False — задача уже финализирована."""
    return await _request_cancel(session, ImportJob, ImportJob.report, job_id)


async def request_export_cancel(session: AsyncSession, job_id: UUID) -> bool:
    """Запрос отмены экспорта до публикации артефакта."""
    return await _request_cancel(session, ExportJob, ExportJob.progress, job_id)


async def _request_cancel(session: AsyncSession, model, column, job_id: UUID) -> bool:
    result = await session.execute(
        update(model)
        .where(model.id == job_id, model.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]))
        .values(
            {
                column: func.jsonb_set(
                    func.coalesce(column, _EMPTY_JSONB), _CANCEL_PATH, func.to_jsonb(true())
                )
            }
        )
    )
    await session.commit()
    return result.rowcount == 1


async def import_cancel_requested(session: AsyncSession, job_id: UUID) -> bool:
    """Проверка флага отмены задачи импорта (читает воркер между строками)."""
    return await _cancel_requested(session, ImportJob, ImportJob.report, job_id)


async def export_cancel_requested(session: AsyncSession, job_id: UUID) -> bool:
    return await _cancel_requested(session, ExportJob, ExportJob.progress, job_id)


async def _cancel_requested(session: AsyncSession, model, column, job_id: UUID) -> bool:
    value = (
        await session.execute(
            select(func.jsonb_extract_path_text(column, CANCEL_REQUEST_KEY)).where(
                model.id == job_id
            )
        )
    ).scalar()
    return str(value).lower() == "true"


async def update_import_counters(
    session: AsyncSession,
    job_id: UUID,
    *,
    total_rows: int | None = None,
    success_rows: int | None = None,
    failed_rows: int | None = None,
) -> None:
    """Прогресс импорта: счётчики строк отдельной короткой транзакцией."""
    values: dict = {}
    if total_rows is not None:
        values["total_rows"] = total_rows
    if success_rows is not None:
        values["success_rows"] = success_rows
    if failed_rows is not None:
        values["failed_rows"] = failed_rows
    if not values:
        return
    await session.execute(update(ImportJob).where(ImportJob.id == job_id).values(**values))
    await session.commit()


async def update_export_progress(session: AsyncSession, job_id: UUID, progress: dict) -> None:
    """Прогресс экспорта: серверное слияние JSONB (cancel_requested сохраняется)."""
    merged = func.coalesce(ExportJob.progress, _EMPTY_JSONB).op("||")(
        bindparam("progress_patch", type_=JSONB)
    )
    await session.execute(
        update(ExportJob).where(ExportJob.id == job_id).values(progress=merged),
        {"progress_patch": progress},
    )
    await session.commit()


async def finalize_import_job(
    session: AsyncSession,
    job_id: UUID,
    *,
    status: JobStatus,
    report: dict | None = None,
    success_rows: int | None = None,
    failed_rows: int | None = None,
) -> None:
    """Перевод задачи импорта в терминальный статус с итожным отчётом."""
    values: dict = {"status": status, "finished_at": datetime.now(UTC)}
    if report is not None:
        values["report"] = report
    if success_rows is not None:
        values["success_rows"] = success_rows
    if failed_rows is not None:
        values["failed_rows"] = failed_rows
    await session.execute(update(ImportJob).where(ImportJob.id == job_id).values(**values))
    await session.commit()


async def finalize_export_job(
    session: AsyncSession,
    job_id: UUID,
    *,
    status: JobStatus,
    file_id: UUID | None = None,
    progress: dict | None = None,
) -> None:
    """Перевод задачи экспорта в терминальный статус (артефакт + счётчики)."""
    values: dict = {"status": status, "finished_at": datetime.now(UTC)}
    if file_id is not None:
        values["file_id"] = file_id
    if progress is not None:
        values["progress"] = progress
    await session.execute(update(ExportJob).where(ExportJob.id == job_id).values(**values))
    await session.commit()


async def log_import_error(
    session: AsyncSession,
    job_id: UUID,
    *,
    row_number: int | None,
    error: str,
    raw_data: dict | None = None,
) -> None:
    """Строка отчёта об ошибках импорта в ``import_errors``.

    ``raw_data`` — только обезличенный снимок строки (см. worker): секреты
    в него не попадают.
    """
    session.add(ImportError(job_id=job_id, row_number=row_number, error=error, raw_data=raw_data))
    await session.commit()
