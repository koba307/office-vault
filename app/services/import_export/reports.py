"""Отчёты по фоновым задачам импорта/экспорта (Phase 08).

Модуль читает состояние ``import_jobs``/``export_jobs`` и собирает
агрегированный отчёт, пригодный для показа в боте. Секреты в отчёт не
попадают: для импорта — только номер строки и причина ошибки, для экспорта —
имя и размер артефакта.
"""

import html
from collections import Counter
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ExportJob, File, ImportError, ImportJob
from app.services.import_export import jobs

MAX_REPORT_ERRORS = 20


def _status_value(status: Any) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _duration_seconds(created_at, finished_at) -> float | None:
    if created_at is None or finished_at is None:
        return None
    if finished_at.tzinfo is None or created_at.tzinfo is None:
        return None
    return round((finished_at - created_at).total_seconds(), 1)


def _cancel_requested(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return str(value.get(jobs.CANCEL_REQUEST_KEY)).lower() == "true"


def build_import_report(job: ImportJob, errors: list[ImportError] | tuple) -> dict:
    """Чистый построитель отчёта по импорту (без БД)."""
    sorted_errors = sorted(errors, key=lambda item: (item.row_number or 0, str(item.id)))
    error_items = [{"row_number": err.row_number, "error": err.error} for err in sorted_errors]
    kinds = Counter(err.error.split(":")[0].strip() for err in sorted_errors if err.error)
    report = job.report if isinstance(job.report, dict) else None
    return {
        "job_id": str(job.id),
        "status": _status_value(job.status),
        "source_format": job.source_format,
        "total_rows": int(job.total_rows or 0),
        "success_rows": int(job.success_rows or 0),
        "failed_rows": int(job.failed_rows or 0),
        "cancel_requested": _status_value(job.status) == "CANCELLED" or _cancel_requested(job.report),
        "errors": error_items[:MAX_REPORT_ERRORS],
        "errors_total": len(error_items),
        "errors_truncated": len(error_items) > MAX_REPORT_ERRORS,
        "error_groups": dict(kinds),
        "fatal_error": (report or {}).get("error") if report else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "duration_seconds": _duration_seconds(job.created_at, job.finished_at),
    }


async def generate_import_report(job_id: UUID, session: AsyncSession) -> dict:
    """Загружает задачу и её ошибки из БД и собирает отчёт."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise ValueError("Задача импорта не найдена.")
    result = await session.execute(
        select(ImportError).where(ImportError.job_id == job_id).order_by(ImportError.row_number.asc().nullslast())
    )
    errors = list(result.scalars().all())
    return build_import_report(job, errors)


async def save_import_report(job_id: UUID, session: AsyncSession) -> dict:
    """Генерирует отчёт и сохраняет его в ``import_jobs.report``."""
    report = await generate_import_report(job_id, session)
    await session.execute(
        sa.update(ImportJob).where(ImportJob.id == job_id).values(report=report)
    )
    await session.commit()
    return report


async def generate_export_report(job_id: UUID, session: AsyncSession) -> dict:
    """Отчёт по экспорту: формат, счётчики, артефакт."""
    job = await session.get(ExportJob, job_id)
    if job is None:
        raise ValueError("Задача экспорта не найдена.")
    progress = job.progress if isinstance(job.progress, dict) else {}
    file = None
    if job.file_id is not None:
        file = await session.get(File, job.file_id)
    return {
        "job_id": str(job.id),
        "status": _status_value(job.status),
        "format": job.format,
        "records": progress.get("written") or progress.get("records"),
        "total_rows": progress.get("total"),
        "file_name": file.original_name if file else progress.get("file_name"),
        "file_size": file.size if file else progress.get("file_size"),
        "sha256": file.sha256 if file else progress.get("sha256"),
        "cancel_requested": _cancel_requested(job.progress),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "duration_seconds": _duration_seconds(job.created_at, job.finished_at),
    }


def format_import_report_text(report: dict) -> str:
    """HTML-блок отчёта по импорту (без секретов)."""
    lines = [
        "📥 <b>Отчёт по импорту</b>",
        f"Статус: <b>{_escape(_status_label(report.get('status')))}</b>",
        f"Формат: {_escape(report.get('source_format') or '—')}",
        f"Всего строк: {report.get('total_rows', 0)}",
        f"Успешно: {report.get('success_rows', 0)}",
        f"Ошибок: {report.get('failed_rows', 0)}",
    ]
    if report.get("fatal_error"):
        lines.append(f"Причина: {_escape(report['fatal_error'])}")
    if report.get("duration_seconds") is not None:
        lines.append(f"Время: {report['duration_seconds']} с")
    errors = report.get("errors") or []
    if errors:
        lines.append("")
        lines.append(f"Ошибки ({report.get('errors_total', len(errors))} из {report.get('total_rows', 0)}):")
        for item in errors:
            lines.append(
                f"· строка {item.get('row_number') or '?'}: {_escape(item.get('error', ''))}"
            )
        if report.get("errors_truncated"):
            lines.append("… остальные ошибки смотрите в таблице import_errors.")
    return "\n".join(lines)


def format_export_report_text(report: dict) -> str:
    lines = [
        "📤 <b>Отчёт по экспорту</b>",
        f"Статус: <b>{_escape(_status_label(report.get('status')))}</b>",
        f"Формат: {_escape(report.get('format') or '—')}",
        f"Записей: {report.get('records') or report.get('total_rows') or 0}",
    ]
    if report.get("file_name"):
        lines.append(f"Файл: {_escape(report['file_name'])}")
    if report.get("file_size") is not None:
        lines.append(f"Размер: {_format_size(int(report['file_size']))}")
    if report.get("duration_seconds") is not None:
        lines.append(f"Время: {report['duration_seconds']} с")
    return "\n".join(lines)


def _escape(value) -> str:
    return html.escape(str(value), quote=False)


def _status_label(status: Any) -> str:
    mapping = {
        "PENDING": "ожидает",
        "PROCESSING": "выполняется",
        "COMPLETED": "завершён",
        "FAILED": "ошибка",
        "CANCELLED": "отменён",
    }
    return mapping.get(str(status).upper(), str(status))


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} Б"
    if size < 1024 ** 2:
        return f"{size / 1024:.1f} КБ"
    return f"{size / 1024 ** 2:.1f} МБ"
