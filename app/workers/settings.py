from typing import ClassVar

from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from app.workers.tasks import backup_records, cleanup_archived


def _redis_settings() -> RedisSettings:
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
    functions: ClassVar[list] = [backup_records, cleanup_archived]
    redis_settings: ClassVar = _redis_settings()
    cron_jobs: ClassVar[list] = [
        cron(backup_records, hour=2, minute=0),
        cron(cleanup_archived, hour=3, minute=0),
    ]