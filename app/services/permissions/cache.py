import json
import logging
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.permissions.versions import read_versions

logger = logging.getLogger(__name__)

# TTL — страховка на случай пропущенной инвалидации, основная защита — версии в БД
CACHE_TTL_SECONDS = 60
KEY_PREFIX = "permissions:context:"

_client: Redis | None = None


def _redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def _key(user_id: UUID) -> str:
    return f"{KEY_PREFIX}{user_id}"


async def get_cached(user_id: UUID, session: AsyncSession) -> dict | None:
    """Возвращает закешированные данные, если версии пользователя не изменились."""
    try:
        raw = await _redis().get(_key(user_id))
    except RedisError:
        logger.warning("Redis недоступен, права читаются из БД", exc_info=True)
        return None
    if raw is None:
        return None

    entry = json.loads(raw)
    # Запись со старыми версиями считается промахом, даже если Redis не удалось очистить
    if entry.get("versions") != await read_versions(session, user_id):
        return None
    return entry["data"]


async def set_cached(user_id: UUID, data: dict, session: AsyncSession) -> None:
    payload = json.dumps({"versions": await read_versions(session, user_id), "data": data})
    try:
        await _redis().set(_key(user_id), payload, ex=CACHE_TTL_SECONDS)
    except RedisError:
        logger.warning("Не удалось записать кеш прав в Redis", exc_info=True)


async def invalidate(user_id: UUID) -> None:
    try:
        await _redis().delete(_key(user_id))
    except RedisError:
        logger.warning("Не удалось сбросить кеш прав пользователя", exc_info=True)


async def invalidate_global() -> None:
    try:
        redis = _redis()
        async for key in redis.scan_iter(match=f"{KEY_PREFIX}*"):
            await redis.delete(key)
    except RedisError:
        logger.warning("Не удалось сбросить глобальный кеш прав", exc_info=True)
