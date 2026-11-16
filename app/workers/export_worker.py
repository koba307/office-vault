"""ARQ-воркер фонового экспорта (Phase 08).

Задача ``run_export_job(job_id)`` запускается воркером ARQ и делегирует
выполнение модулю :mod:`app.services.import_export.export_jobs`
(``run_export_job``): сбор записей, формирование файла, шифрование и
публикация в S3 на время срока хранения. Воркер отвечает за:

* **Идемпотентность** — задача обрабатывается только в статусе ``PENDING``;
  повторный запуск ничего не делает.
* **Прогресс/отчёт** — заполняется внутри ``run_export_job``.
* **Аудит** — финал задачи пишется в ``audit_logs``.
* **Безопасное временное хранение** — осиротевшие файлы добирает
  периодическая задача ``cleanup_export_temp``.
"""

import asyncio
import tempfile
from pathlib import Path
from uuid import UUID

from arq.connections import create_pool

from app.database.models import ExportJob, JobStatus, User
from app.database.session import async_session
from app.services.auth.permissions import log_audit
from app.services.files.service import read_object
from app.services.import_export import export_jobs as export_jobs_service

EXPORT_TEMP_ROOT = Path(tempfile.gettempdir()) / "office_vault_exports"


async def enqueue_export_job(job_id) -> bool:
    """Ставит экспорт в очередь ARQ. False — Redis недоступен.

    Ключ очереди совпадает с id задачи: повторная постановка той же задачи
    не создаёт второго экземпляра (дедупликация средствами ARQ).
    """
    from app.workers.settings import build_redis_settings

    pool = None
    try:
        pool = await create_pool(build_redis_settings())
        await pool.enqueue_job("run_export_job", str(job_id), job_id=f"run_export_job:{job_id}")
        return True
    except Exception:  # noqa: BLE001 — Redis недоступен
        return False
    finally:
        if pool is not None:
            await pool.close(close_connection_pool=True)


async def load_export_payload(session, job: ExportJob):
    """Читает опубликованный артефакт задачи (для отдачи в боте)."""
    options = job.filters or {}
    storage_key = options.get("storage_key")
    bucket = options.get("bucket")
    if not storage_key or not bucket:
        raise ValueError("Файл экспорта ещё не готов.")
    data = await asyncio.to_thread(read_object, bucket, storage_key)
    return bucket, storage_key, data


async def run_export_job(ctx, job_id: str) -> str:
    """Фоновый экспорт: сбор записей → файл → публикация в S3."""
    try:
        key = UUID(str(job_id))
    except (TypeError, ValueError):
        return "Bad job id"

    async with async_session() as control:
        job = await control.get(ExportJob, key)
        if job is None:
            return f"Job {key} not found"
        if job.status != JobStatus.PENDING:
            # Идемпотентность: повторный запуск ничего не делает.
            return f"Job {key} is already processed ({job.status.value})"
        user = await control.get(User, job.user_id)

    if user is None:
        return await _fail(key, None, "Пользователь задачи экспорта не найден.")

    # Выполняем экспорт в одной сессии (run_export_job сам управляет транзакцией).
    async with async_session() as session:
        try:
            artifact = await export_jobs_service.run_export_job(
                session, key, user=user, progress=None
            )
        except export_jobs_service.ExportJobError as exc:
            message = str(exc)
            if "уже выполнена" in message or "уже отменена" in message or "уже завершена" in message:
                return f"Job {key} is already processed"
            return await _fail(key, user.id, message)
        except Exception as exc:  # noqa: BLE001
            return await _fail(key, user.id, f"Фатальная ошибка экспорта: {exc}")

    options = (artifact and {}) or {}
    try:
        async with async_session() as control:
            fresh = await control.get(ExportJob, key)
            options = fresh.filters if (fresh and isinstance(fresh.filters, dict)) else {}
    except Exception:  # noqa: S110, BLE001
        pass

    record_count = options.get("record_count", 0)
    file_name = options.get("filename")
    await _audit(user.id, key, "EXPORT_JOB_COMPLETED", "SUCCESS", record_count, file_name)
    return f"Export {key} done: {record_count} records, file {file_name}"


async def cleanup_export_temp(ctx=None, max_age_seconds: int = 6 * 3600) -> str:
    """Автоудаление осиротевших временных файлов (cron-задача).

    Приватный каталог временных файлов экспорта очищается от файлов старше
    ``max_age_seconds``; каталог создаётся при необходимости.
    """

    root = EXPORT_TEMP_ROOT
    if not root.is_dir():
        return "No export temp directory"
    import time

    threshold = time.time() - max_age_seconds
    removed = 0
    for path in root.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < threshold:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return f"Removed {removed} stale export temp files"


async def _audit(user_id, job_id: UUID, action: str, result: str, records: int, name) -> None:
    async with async_session() as control:
        await log_audit(
            control,
            user_id,
            action,
            "ExportJob",
            str(job_id),
            result=result,
            metadata={"records": records, "file_name": name},
        )


async def _fail(job_id: UUID, user_id, error: str) -> str:
    async with async_session() as control:
        from sqlalchemy import update as sa_update

        from app.database.models import ExportJob, JobStatus

        await control.execute(
            sa_update(ExportJob).where(ExportJob.id == job_id).values(
                status=JobStatus.FAILED, filters={"error": error}
            )
        )
        await control.commit()
    async with async_session() as control:
        await log_audit(
            control, user_id, "EXPORT_JOB_FAILED", "ExportJob", str(job_id),
            result="FAILED", metadata={"error": error},
        )
    return f"Export {job_id} failed: {error}"
