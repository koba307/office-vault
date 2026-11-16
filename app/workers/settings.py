from typing import ClassVar

from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from app.workers.export_worker import cleanup_export_temp, run_export_job
from app.workers.import_worker import run_import_job
from app.workers.tasks import backup_records, cleanup_archived, cleanup_expired_exports


def build_redis_settings() -> RedisSettings:
    url = settings.redis_url
    # arq expects host/port/db
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int((parsed.path or "/0").lstrip("/") or 0),
        password=parsed.password,
    )


class WorkerSettings:
    functions: ClassVar[list] = [
        backup_records,
        cleanup_archived,
        cleanup_expired_exports,
        run_import_job,
        run_export_job,
        cleanup_export_temp,
    ]
    redis_settings: ClassVar = build_redis_settings()
    cron_jobs: ClassVar[list] = [
        cron(backup_records, hour=2, minute=0),
        cron(cleanup_archived, hour=3, minute=0),
        cron(cleanup_expired_exports, hour=3, minute=30),
        cron(cleanup_export_temp, hour=4, minute=0),
    ]