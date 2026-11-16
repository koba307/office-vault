"""Фоновые задачи импорта/экспорта (Phase 08): ARQ-воркеры и жизненный цикл.

Чистые тесты проверяют API воркеров и служб без базы.
Интеграционные используют ``TEST_DATABASE_URL`` (изолированная PostgreSQL
с применёнными миграциями и seed) и проверяют полный прогон задачи,
идемпотентность, отчёт об ошибках и очистку временных файлов.
"""

import asyncio
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.models import (
    AuditLog,
    Category,
    ExportJob,
    File,
    ImportError,
    ImportJob,
    JobStatus,
    Record,
    RecordField,
    Role,
    RoleCode,
    User,
    UserStatus,
)
from app.services.import_export import export_jobs as export_jobs_service
from app.services.import_export import jobs as import_export_jobs
from app.services.import_export import service as import_export_service
from app.workers import export_worker, import_worker


# ---------------------------------------------------------------------------
# Чистые тесты: API воркеров и служб (без БД)
# ---------------------------------------------------------------------------


def test_worker_functions_are_arq_tasks():
    import inspect

    for func in (import_worker.run_import_job, export_worker.run_export_job):
        assert inspect.iscoroutinefunction(func)
        params = list(inspect.signature(func).parameters)
        assert params[0] == "ctx"
        assert params[1] == "job_id"


def test_enqueue_helpers_are_coroutines():
    import inspect

    for func in (import_worker.enqueue_import_job, export_worker.enqueue_export_job):
        assert inspect.iscoroutinefunction(func)


def test_jobs_module_api_exists():
    import inspect

    for name in (
        "claim_import_job",
        "claim_export_job",
        "request_import_cancel",
        "request_export_cancel",
        "import_cancel_requested",
        "export_cancel_requested",
        "update_import_counters",
        "update_export_progress",
        "finalize_import_job",
        "finalize_export_job",
        "log_import_error",
        "store_import_source",
        "create_import_job",
        "create_export_job",
        "detect_import_format",
    ):
        func = getattr(import_export_jobs, name)
        assert inspect.iscoroutinefunction(func) or callable(func), name


def test_reports_module_api_exists():
    import inspect

    from app.services.import_export import reports

    for name in ("generate_import_report", "generate_export_report", "save_import_report",
                 "build_import_report", "format_import_report_text", "format_export_report_text"):
        func = getattr(reports, name)
        assert callable(func), name
        if name.startswith("generate_") or name == "save_import_report":
            assert inspect.iscoroutinefunction(func), name


def test_detect_import_format_by_extension():
    assert import_export_jobs.detect_import_format("data.csv") == "CSV"
    assert import_export_jobs.detect_import_format("data.tsv") == "TSV"
    assert import_export_jobs.detect_import_format("data.json") == "JSON"
    assert import_export_jobs.detect_import_format("data.jsonl") == "JSONL"
    assert import_export_jobs.detect_import_format("data.txt") == "TXT"
    assert import_export_jobs.detect_import_format("data.xlsx") is None
    assert import_export_jobs.detect_import_format(None) is None


def test_cleanup_export_temp_removes_stale_files(tmp_path, monkeypatch):
    monkeypatch.setattr(export_worker, "EXPORT_TEMP_ROOT", tmp_path)
    root = tmp_path
    root.mkdir(mode=0o700, exist_ok=True)
    stale = root / "export_old.tmp"
    stale.write_text("x")
    # сделать файл «старым»
    import os
    import time

    os.utime(stale, (time.time() - 10_000, time.time() - 10_000))
    fresh = root / "export_fresh.tmp"
    fresh.write_text("y")

    removed = asyncio.run(export_worker.cleanup_export_temp(max_age_seconds=100))
    assert "Removed 1" in removed
    assert not stale.exists()
    assert fresh.exists()


def test_build_import_report_redacts_and_caps():
    from types import SimpleNamespace

    from app.services.import_export import reports

    job = SimpleNamespace(
        id=uuid4(),
        status=JobStatus.COMPLETED,
        source_format="CSV",
        total_rows=5,
        success_rows=3,
        failed_rows=2,
        report=None,
        created_at=None,
        finished_at=None,
    )
    errors = [
        SimpleNamespace(id=uuid4(), row_number=i, error=f"err {i}") for i in range(1, 25)
    ]
    report = reports.build_import_report(job, errors)
    assert report["total_rows"] == 5
    assert report["success_rows"] == 3
    assert report["failed_rows"] == 2
    assert len(report["errors"]) == reports.MAX_REPORT_ERRORS
    assert report["errors_truncated"] is True


# ---------------------------------------------------------------------------
# Интеграционные тесты: PostgreSQL (TEST_DATABASE_URL)
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_session():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated PostgreSQL test database")

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as session:
            try:
                await session.execute(select(Role.id).limit(1))
            except SQLAlchemyError:
                pytest.skip("Apply migrations and seed the test database first")
            yield session
    finally:
        await engine.dispose()


async def _superadmin(session: AsyncSession) -> User:
    user = (
        await session.execute(select(User).where(User.role == RoleCode.SUPERADMIN).limit(1))
    ).scalar_one_or_none()
    if user is None:
        pytest.skip("The test database has no seeded SUPERADMIN user")
    return user


async def _make_category(session: AsyncSession, code_prefix: str = "ie") -> Category:
    category = Category(
        code=f"{code_prefix}_{uuid4().hex[:10]}",
        name="IE Test Category",
        category_type="root",
        status="ACTIVE",
        sort_order=99,
    )
    session.add(category)
    await session.commit()
    await session.refresh(category)
    return category


async def _store_import_file(monkeypatch, session, user, data: bytes, filename: str = "import.csv") -> File:
    storage: dict[str, bytes] = {}

    def fake_store(payload, key, content_type):
        storage[key] = bytes(payload)
        return "test-bucket"

    monkeypatch.setattr(import_export_jobs, "store_object", fake_store)
    file = await import_export_jobs.store_import_source(
        session, user=user, data=data, filename=filename, content_type="text/csv"
    )
    # сохраняем хранилище в атрибуте файла для удобства (не обязательно)
    file._storage = storage  # type: ignore[attr-defined]
    return file


async def _cleanup_import(session, job_id, user_id, category_ids, record_ids=None):
    if record_ids:
        await session.execute(delete(RecordField).where(RecordField.record_id.in_(list(record_ids))))
        await session.execute(delete(Record).where(Record.id.in_(list(record_ids))))
    await session.execute(delete(ImportError).where(ImportError.job_id == job_id))
    await session.execute(delete(ImportJob).where(ImportJob.id == job_id))
    await session.execute(delete(AuditLog).where(AuditLog.object_id == str(job_id)))
    if category_ids:
        await session.execute(delete(Category).where(Category.id.in_(list(category_ids))))
    await session.execute(delete(File).where(File.uploaded_by == user_id))
    await session.execute(delete(User).where(User.id == user_id))
    await session.commit()


async def test_import_job_end_to_end(db_session, monkeypatch):
    # Используем сидированного суперадмина
    user = await _superadmin(db_session)
    category = await _make_category(db_session)
    csv_text = "title,type,description\nAlpha,OTHER,first\nBeta,OTHER,second\n"
    file = await _store_import_file(monkeypatch, db_session, user, csv_text.encode(), "import.csv")
    job = await import_export_service.create_import_job(
        db_session, user, category.code, file.id, source_format="CSV"
    )
    # чистим state сессии
    await db_session.commit()

    result = await import_worker.run_import_job(None, str(job.id))
    assert "done" in result

    refreshed = await db_session.get(ImportJob, job.id)
    assert refreshed.status == JobStatus.COMPLETED
    assert refreshed.total_rows == 2
    assert refreshed.success_rows == 2
    assert refreshed.failed_rows == 0

    records = (
        await db_session.execute(select(Record).where(Record.category_id == category.id))
    ).scalars().all()
    assert len(records) == 2

    await _cleanup_import(db_session, job.id, user.id, [category.id], [r.id for r in records])


async def test_import_job_idempotent(db_session, monkeypatch):
    user = await _superadmin(db_session)
    category = await _make_category(db_session)
    csv_text = "title,type\nOnly,OTHER\n"
    file = await _store_import_file(monkeypatch, db_session, user, csv_text.encode())
    job = await import_export_service.create_import_job(
        db_session, user, category.code, file.id, source_format="CSV"
    )
    await db_session.commit()

    first = await import_worker.run_import_job(None, str(job.id))
    assert "done" in first
    second = await import_worker.run_import_job(None, str(job.id))
    assert "already processed" in second

    records = (
        await db_session.execute(select(Record).where(Record.category_id == category.id))
    ).scalars().all()
    assert len(records) == 1

    await _cleanup_import(db_session, job.id, user.id, [category.id], [r.id for r in records])


async def test_import_job_reports_errors(db_session, monkeypatch):
    user = await _superadmin(db_session)
    category = await _make_category(db_session)
    # Дубликат названия внутри файла + пустое имя → ошибки
    csv_text = "title,type\nDup,OTHER\nDup,OTHER\n,OTHER\n"
    file = await _store_import_file(monkeypatch, db_session, user, csv_text.encode())
    job = await import_export_service.create_import_job(
        db_session, user, category.code, file.id, source_format="CSV"
    )
    await db_session.commit()

    result = await import_worker.run_import_job(None, str(job.id))
    assert "done" in result

    refreshed = await db_session.get(ImportJob, job.id)
    assert refreshed.status == JobStatus.COMPLETED
    assert refreshed.success_rows == 1
    assert refreshed.failed_rows >= 2

    errors = (
        await db_session.execute(select(ImportError).where(ImportError.job_id == job.id))
    ).scalars().all()
    assert len(errors) >= 2

    records = (
        await db_session.execute(select(Record).where(Record.category_id == category.id))
    ).scalars().all()
    await _cleanup_import(db_session, job.id, user.id, [category.id], [r.id for r in records])


async def test_import_job_no_source_file_returns_skip(db_session):
    user = await _superadmin(db_session)
    category = await _make_category(db_session)
    job = await import_export_service.create_import_job(
        db_session, user, category.code, None, source_format="CSV"
    )
    await db_session.commit()
    result = await import_worker.run_import_job(None, str(job.id))
    assert "no stored source file" in result
    # уборка
    await db_session.execute(delete(ImportJob).where(ImportJob.id == job.id))
    await db_session.execute(delete(AuditLog).where(AuditLog.object_id == str(job.id)))
    await db_session.execute(delete(Category).where(Category.id == category.id))
    await db_session.commit()


async def test_export_job_end_to_end(db_session, monkeypatch):
    user = await _superadmin(db_session)
    # создаём пару записей
    category = await _make_category(db_session)
    rec = Record(category_id=category.id, record_type="OTHER", title="ExportMe", status="ACTIVE", created_by=user.id)
    db_session.add(rec)
    await db_session.commit()

    storage: dict[str, bytes] = {}

    def fake_store(payload, key, content_type):
        storage[key] = bytes(payload)
        return "test-bucket"

    def fake_remove(bucket, keys):
        for key in keys:
            storage.pop(key, None)

    monkeypatch.setattr(export_jobs_service, "store_object", fake_store)
    monkeypatch.setattr(export_jobs_service, "remove_objects", fake_remove)

    job = await export_jobs_service.create_export_job(
        db_session, user, category.code, "csv"
    )
    await db_session.commit()

    result = await export_worker.run_export_job(None, str(job.id))
    assert "done" in result

    refreshed = await db_session.get(ExportJob, job.id)
    assert refreshed.status == JobStatus.COMPLETED
    opts = refreshed.filters or {}
    assert opts.get("storage_key")
    assert opts.get("record_count", 0) >= 1

    # повторный запуск — идемпотентность
    second = await export_worker.run_export_job(None, str(job.id))
    assert "already processed" in second

    # уборка
    await db_session.execute(delete(Record).where(Record.id == rec.id))
    await db_session.execute(delete(ExportJob).where(ExportJob.id == job.id))
    await db_session.execute(delete(AuditLog).where(AuditLog.object_id == str(job.id)))
    await db_session.execute(delete(Category).where(Category.id == category.id))
    await db_session.commit()


async def test_export_job_cancel_requested_flag(db_session):
    job_id = None
    # проверяем флаг отмены на уровне jobs (без воркера)
    user = await _superadmin(db_session)
    category = await _make_category(db_session)
    job = await import_export_jobs.create_export_job(
        db_session, user=user, export_format="CSV", category=category
    )
    job_id = job.id
    ok = await import_export_jobs.request_export_cancel(db_session, job_id)
    assert ok is True
    assert await import_export_jobs.export_cancel_requested(db_session, job_id) is True
    # повторный запрос — задача уже? статус PENDING, но флаг уже стоит; повторно ставим — идемпотентно True
    # уборка
    await db_session.execute(delete(ExportJob).where(ExportJob.id == job_id))
    await db_session.execute(delete(Category).where(Category.id == category.id))
    await db_session.commit()
