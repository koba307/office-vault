from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Category, Favorite, PermissionCode, Record, User
from app.services.permissions.context import load_access_context
from app.services.permissions.record import check_record_permission, record_denial_reason


class FavoriteError(ValueError):
    """Ошибка избранного: запись не найдена или недоступна пользователю."""


@dataclass(frozen=True)
class FavoriteItem:
    record: Record
    category_name: str
    added_at: datetime | None


async def add_favorite(session: AsyncSession, user_id: UUID, record_id: UUID) -> bool:
    """Добавляет запись в избранное. Возвращает False, если она уже там.

    Доступ проверяется в момент добавления; в избранное попадают только активные записи.
    """
    record = await session.get(Record, record_id)
    if record is None or record.status != "ACTIVE":
        raise FavoriteError("Запись не найдена")
    user = await session.get(User, user_id)
    if not await check_record_permission(user, record_id, PermissionCode.VIEW, session):
        raise FavoriteError("Нет доступа к записи")
    if await session.get(Favorite, (user_id, record_id)) is not None:
        return False
    session.add(Favorite(user_id=user_id, record_id=record_id))
    await session.commit()
    return True


async def remove_favorite(session: AsyncSession, user_id: UUID, record_id: UUID) -> bool:
    """Убирает запись из избранного. Возвращает False, если её там не было."""
    result = await session.execute(
        delete(Favorite).where(Favorite.user_id == user_id, Favorite.record_id == record_id)
    )
    await session.commit()
    return result.rowcount > 0


async def is_favorite(session: AsyncSession, user_id: UUID, record_id: UUID) -> bool:
    return await session.get(Favorite, (user_id, record_id)) is not None


async def list_favorites(
    session: AsyncSession,
    user_id: UUID,
    *,
    limit: int = 10,
    offset: int = 0,
) -> tuple[list[FavoriteItem], int]:
    """Страница избранного и общее число записей, к которым доступ ещё есть.

    Доступ проверяется заново для каждой записи: если его отозвали, запись
    исключается из выдачи и из счётчика. Строка избранного при этом не удаляется,
    поэтому запись вернётся, если доступ восстановят.
    """
    user = await session.get(User, user_id)
    ctx, _ = await load_access_context(user, session)
    if ctx is None:
        return [], 0

    result = await session.execute(
        select(Record, Category.name, Favorite.created_at)
        .join(Favorite, Favorite.record_id == Record.id)
        .join(Category, Category.id == Record.category_id)
        .where(Favorite.user_id == user_id, Record.status == "ACTIVE")
        .order_by(Favorite.created_at.desc(), Record.id)
    )
    allowed: list[FavoriteItem] = []
    for record, category_name, added_at in result.all():
        if await record_denial_reason(ctx, record.id, PermissionCode.VIEW, session) is None:
            allowed.append(FavoriteItem(record=record, category_name=category_name, added_at=added_at))
    return allowed[offset : offset + limit], len(allowed)
