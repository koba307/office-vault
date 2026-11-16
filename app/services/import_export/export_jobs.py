"""Задачи экспорта (Phase 08): создание, выполнение, статус и очистка готовых файлов.

Готовый файл хранится в объектном хранилище в зашифрованном виде (AES-256-GCM)
ограниченное время (``EXPORT_RETENTION_HOURS``). Очистку выполняет воркер; после очистки
задача остаётся в журнале с отметкой ``purged_at``, а файла больше нет.

Модуль выделен отдельно от ``service.py``, который ведёт задачи импорта.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.encryption import encrypt_bytes
from app.database.models import ExportJob, JobStatus, RecordType, RoleCode, User
from app.services.files.service import remove_objects, store_object
from app.services.import_export.export import (
    ExportError,
    ensure_export_rights,
    export_records,
    normalize_format,
    normalize_record_type_filter,
    parse_record_ids,
    resolve_category_code,
)
from app.services.import_export.export_formats import FORMAT_EXTENSIONS

logger = logging.getLogger(__name__)

EXPORT_ARTIFACT_PREFIX = "exports"
ARTIFACT_CONTENT_TYPE = "application/octet-stream"
ProgressCallback = Callable[[int, str], Awaitable[None]]


class ExportJobError(ExportError):
    """Задачу экспорта нельзя создать или выполнить."""


@dataclass(frozen=True)
class ExportArtifact:
    """Результат выполнения задачи: открытые байты для отправки и итоги для отчёта."""

    job_id: UUID
    data: bytes
    filename: str
    mime_type: str
    format: str
    record_count: int
    file_count: int
    include_secrets: bool
    skipped_files: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    expires_at: datetime | None = None


def export_expires_at(finished_at: datetime, retention_hours: int | None = None) -> datetime:
    """Момент, после которого готовый файл экспорта удаляется из хранилища."""
    hours = settings.export_retention_hours if retention_hours is None else retention_hours
    return finished_at + timedelta(hours=hours)


async def _report(progress: ProgressCallback | None, percent: int, text: str) -> None:
    """Сбой обновления прогресса не должен срывать задачу."""
    if progress is None:
        return
    try:
        await progress(percent, text)
    except Exception:
        logger.debug("Не удалось обновить прогресс экспорта", exc_info=True)


def _safe_error(exc: Exception) -> str:
    """В журнал задачи попадает только безопасное сообщение, без деталей внутренних ошибок."""
    if isinstance(exc, ExportError):
        return str(exc)
    return "Внутренняя ошибка экспорта."


async def create_export_job(
    session: AsyncSession,
    user: User | None,
    category_code: str | None,
    format: str,
    *,
    record_type: RecordType | str | None = None,
    record_ids: Sequence[UUID | str] | None = None,
    fields: Sequence[str] | None = None,
    include_files: bool = False,
    include_secrets: bool = False,
) -> ExportJob:
    """Создаёт задачу экспорта в статусе PENDING. Права проверяются сразу, до постановки."""
    if user is None:
        raise ExportJobError("Пользователь не найден.")
    fmt = normalize_format(format)
    category = await resolve_category_code(session, category_code)
    rtype = normalize_record_type_filter(record_type)
    ids = parse_record_ids(record_ids)
    await ensure_export_rights(session, user, category=category, include_secrets=include_secrets)

    job = ExportJob(
        user_id=user.id,
        category_id=category.id if category else None,
        status=JobStatus.PENDING,
        format=fmt,
        filters={
            "category_code": category.code if category else None,
            "record_type": rtype.value if rtype else None,
            "record_ids": [str(item) for item in ids] if ids is not None else None,
            "fields": list(fields) if fields is not None else None,
            "include_files": bool(include_files),
            "include_secrets": bool(include_secrets),
        },
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def run_export_job(
    session: AsyncSession,
    job_id: UUID,
    *,
    user: User,
    progress: ProgressCallback | None = None,
) -> ExportArtifact:
    """Выполняет задачу: собирает файл, шифрует и сохраняет копию, фиксирует результат.

    Задача принадлежит тому, кто её создал. Права проверяются повторно при выполнении.
    При ошибке задача переходит в FAILED, а исключение пробрасывается вызывающему коду.
    """
    user_id = user.id
    job = await session.get(ExportJob, job_id)
    if job is None or job.user_id != user_id:
        raise ExportJobError("Задача экспорта не найдена.")
    if job.status != JobStatus.PENDING:
        raise ExportJobError("Задача уже выполнена или отменена.")

    options = dict(job.filters or {})
    fmt = job.format
    job.status = JobStatus.PROCESSING
    await session.commit()

    await _report(progress, 10, "Проверяю права и собираю записи")
    try:
        result = await export_records(
            session,
            user,
            category_code=options.get("category_code"),
            record_type=options.get("record_type"),
            record_ids=options.get("record_ids"),
            fields=options.get("fields"),
            format=fmt,
            include_files=bool(options.get("include_files")),
            include_secrets=bool(options.get("include_secrets")),
        )

        await _report(progress, 60, "Формирую файл")
        storage_key = f"{EXPORT_ARTIFACT_PREFIX}/{job_id}.{FORMAT_EXTENSIONS[fmt]}.enc"
        try:
            bucket = await asyncio.to_thread(
                store_object, encrypt_bytes(result.data), storage_key, ARTIFACT_CONTENT_TYPE
            )
        except Exception as exc:
            raise ExportJobError("Хранилище экспорта недоступно. Попробуйте позже.") from exc

        await _report(progress, 90, "Сохраняю копию на время срока хранения")
        finished_at = datetime.now(UTC)
        job = await session.get(ExportJob, job_id)
        job.status = JobStatus.COMPLETED
        job.finished_at = finished_at
        job.filters = {
            **options,
            "filename": result.filename,
            "bucket": bucket,
            "storage_key": storage_key,
            "size": len(result.data),
            "sha256": sha256(result.data).hexdigest(),
            "record_count": result.record_count,
            "file_count": result.file_count,
            "skipped_files_count": len(result.skipped_files),
            "columns_count": len(result.columns),
        }
        await session.commit()
    except Exception as exc:
        await session.rollback()
        if not isinstance(exc, ExportError):
            logger.exception("Сбой выполнения задачи экспорта %s", job_id)
        failed = await session.get(ExportJob, job_id)
        failed.status = JobStatus.FAILED
        failed.finished_at = datetime.now(UTC)
        failed.filters = {**options, "error": _safe_error(exc)}
        await session.commit()
        raise

    await _report(progress, 100, "Готово")
    return ExportArtifact(
        job_id=job_id,
        data=result.data,
        filename=result.filename,
        mime_type=result.mime_type,
        format=fmt,
        record_count=result.record_count,
        file_count=result.file_count,
        include_secrets=result.include_secrets,
        skipped_files=result.skipped_files,
        expires_at=export_expires_at(finished_at),
    )


async def get_export_status(
    session: AsyncSession,
    job_id: UUID,
    *,
    user: User | None = None,
) -> dict[str, Any] | None:
    """Статус задачи. Чужую задачу видит только SUPERADMIN; иначе возвращается None."""
    job = await session.get(ExportJob, job_id)
    if job is None:
        return None
    if user is not None and job.user_id != user.id and user.role != RoleCode.SUPERADMIN:
        return None

    options = job.filters or {}
    expires_at = export_expires_at(job.finished_at) if job.finished_at else None
    purged = bool(options.get("purged_at"))
    return {
        "job_id": str(job.id),
        "status": job.status.value,
        "format": job.format,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "expires_at": expires_at,
        "available": job.status == JobStatus.COMPLETED and not purged,
        "expired": purged,
        "record_count": options.get("record_count"),
        "file_count": options.get("file_count"),
        "skipped_files_count": options.get("skipped_files_count"),
        "size": options.get("size"),
        "error": options.get("error"),
    }


async def purge_expired_exports(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Удаляет из хранилища готовые файлы, срок хранения которых истёк. Возвращает число удалённых."""
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(hours=settings.export_retention_hours)
    result = await session.execute(
        select(ExportJob).where(
            ExportJob.status == JobStatus.COMPLETED,
            ExportJob.finished_at.is_not(None),
            ExportJob.finished_at <= cutoff,
            ExportJob.filters["purged_at"].astext.is_(None),
        )
    )
    purged = 0
    for job in result.scalars().all():
        options = dict(job.filters or {})
        storage_key = options.get("storage_key")
        bucket = options.get("bucket")
        if storage_key and bucket:
            try:
                await asyncio.to_thread(remove_objects, bucket, {storage_key})
            except Exception:
                logger.exception("Не удалось удалить файл экспорта %s", job.id)
                continue
        job.filters = {**options, "purged_at": moment.isoformat()}
        purged += 1
    await session.commit()
    return purged
