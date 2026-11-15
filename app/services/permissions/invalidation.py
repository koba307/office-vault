from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.permissions import cache, versions


async def _drop_cached(affected: set[UUID] | None) -> None:
    # None означает, что затронуты все сотрудники
    if affected is None:
        await cache.invalidate_global()
        return
    for user_id in affected:
        await cache.invalidate(user_id)


# Функции не коммитят транзакцию: вызывающий код должен сделать commit после изменения


async def on_group_changed(user_id: UUID, session: AsyncSession) -> None:
    await versions.bump_user_version(user_id, session)
    await cache.invalidate(user_id)


async def on_role_changed(user_id: UUID, session: AsyncSession) -> None:
    await versions.bump_user_version(user_id, session)
    await cache.invalidate(user_id)


async def on_status_changed(user_id: UUID, session: AsyncSession) -> None:
    await versions.bump_user_version(user_id, session)
    await cache.invalidate(user_id)


async def on_category_access_changed(category_id: UUID, session: AsyncSession) -> None:
    affected = await versions.bump_object_version("category", category_id, session)
    await _drop_cached(affected)


async def on_record_access_changed(record_id: UUID, session: AsyncSession) -> None:
    affected = await versions.bump_object_version("record", record_id, session)
    await _drop_cached(affected)
