"""ARQ-воркер фонового импорта (Phase 08).

Задача ``run_import_job(job_id)`` запускается воркером ARQ и делегирует
выполнение модулю :mod:`app.services.import_export.service` (``import_records``):
парсинг форматов, сопоставление полей, дедупликация, savepoint на строку и
отчёт об ошибках. Воркер отвечает за:

* **Идемпотентность** — атомарный захват задачи из ``PENDING`` в
  ``PROCESSING`` (``jobs.claim_import_job``): повторный запуск той же
  задачи ничего не импортирует второй раз.
* **Транзакционность/отмена** — обрабатывается внутри ``import_records``
  (savepoint на строку, откат при ``JobStatus.CANCELLED``).
* **Прогресс/отчёт** — счётчики и ``import_jobs.report`` пишет
  ``import_records``.
* **Аудит** — финал задачи пишется в ``audit_logs``.
"""

import asyncio
from uuid import UUID

from arq.connections import create_pool
from sqlalchemy import update as sa_update

from app.database.models import File, ImportJob, JobStatus, User
from app.database.session import async_session
from app.services.auth.permissions import log_audit
from app.services.files.service import read_object
from app.services.import_export import jobs
from app.services.import_export.service import (
    ImportCancelled,
    ImportJobError,
    import_records,
)


async def enqueue_import_job(job_id) -> bool:
    """Ставит импорт в очередь ARQ. False — Redis недоступен.

    Ключ очереди совпадает с id задачи: повторная постановка той же задачи
    не создаёт второго экземпляра (дедупликация средствами ARQ).
    """
    from app.workers.settings import build_redis_settings

    pool = None
    try:
        pool = await create_pool(build_redis_settings())
        await pool.enqueue_job("run_import_job", str(job_id), job_id=f"run_import_job:{job_id}")
        return True
    except Exception:  # noqa: BLE001 — Redis недоступен
        return False
    finally:
        if pool is not None:
            await pool.close(close_connection_pool=True)


async def run_import_job(ctx, job_id: str) -> str:
    """Фоновый импорт: строки файла-источника → записи категории."""
    try:
        key = UUID(str(job_id))
    except (TypeError, ValueError):
        return "Bad job id"

    async with async_session() as control:
        job = await jobs.claim_import_job(control, key)
        if job is None:
            # Идемпотентность: задача уже захвачена или финализирована.
            return f"Job {key} is already processed"
        user = await control.get(User, job.user_id)
        source_file = await control.get(File, job.file_id) if job.file_id else None

    if user is None:
        return await _fail(key, None, "Пользователь задачи импорта не найден.")
    if source_file is None:
        # Файл не сохранён (импорт выполнялся синхронно в боте) — воркеру нечего делать.
        return f"Job {key} has no stored source file (already handled inline)"

    try:
        payload = await asyncio.to_thread(read_object, source_file.bucket, source_file.storage_key)
    except Exception as exc:  # noqa: BLE001 — хранилище недоступно
        return await _fail(key, user.id, f"Не удалось прочитать файл: {exc}")

    async with async_session() as session:
        try:
            await import_records(
                session,
                key,
                file_bytes=payload,
                filename=source_file.original_name,
                encoding=None,
            )
        except ImportCancelled:
            await _audit(user.id, key, "IMPORT_JOB_CANCELLED", "CANCELLED")
            return f"Import {key} cancelled before commit"
        except ImportJobError as exc:
            message = str(exc)
            if "уже завершена" in message or "уже выполнена" in message:
                return f"Job {key} is already processed"
            return await _fail(key, user.id, message)
        except Exception as exc:  # noqa: BLE001
            return await _fail(key, user.id, f"Фатальная ошибка импорта: {exc}")

    report = None
    async with async_session() as control:
        job = await control.get(ImportJob, key)
        if job is not None and isinstance(job.report, dict):
            report = job.report
        counters = (job.total_rows or 0, job.success_rows or 0, job.failed_rows or 0) if job else (0, 0, 0)
    audit_result = "SUCCESS" if counters[2] == 0 else "PARTIAL"
    if report and report.get("cancelled"):
        audit_result = "CANCELLED"
    await _audit(user.id, key, "IMPORT_JOB_COMPLETED", audit_result, report)
    return f"Import {key} done: {counters[1]} ok, {counters[2]} failed"


async def _audit(user_id, job_id: UUID, action: str, result: str, report: dict | None = None) -> None:
    async with async_session() as control:
        await log_audit(
            control,
            user_id,
            action,
            "ImportJob",
            str(job_id),
            result=result,
            metadata={
                "total_rows": (report or {}).get("total_rows") if isinstance(report, dict) else None,
                "success_rows": (report or {}).get("success_rows") if isinstance(report, dict) else None,
                "failed_rows": (report or {}).get("failed_rows") if isinstance(report, dict) else None,
            },
        )


async def _fail(job_id: UUID, user_id, error: str) -> str:
    """Фатальный сбой: задача FAILED, причина — в отчёте и аудите."""
    async with async_session() as control:
        await control.execute(
            sa_update(ImportJob).where(ImportJob.id == job_id).values(
                status=JobStatus.FAILED, report={"error": error}
            )
        )
        await control.commit()
    async with async_session() as control:
        await log_audit(
            control, user_id, "IMPORT_JOB_FAILED", "ImportJob", str(job_id),
            result="FAILED", metadata={"error": error},
        )
    return f"Import {job_id} failed: {error}"
