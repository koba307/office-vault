from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Favorite, Record


async def add_favorite(session: AsyncSession, user_id: UUID, record_id: UUID) -> None:
    fav = Favorite(user_id=user_id, record_id=record_id)
    session.add(fav)
    await session.commit()


async def remove_favorite(session: AsyncSession, user_id: UUID, record_id: UUID) -> None:
    result = await session.execute(
        select(Favorite).where(Favorite.user_id == user_id, Favorite.record_id == record_id)
    )
    fav = result.scalar_one_or_none()
    if fav:
        await session.delete(fav)
        await session.commit()


async def list_favorites(session: AsyncSession, user_id: UUID) -> list[Record]:
    result = await session.execute(
        select(Record)
        .join(Favorite, Favorite.record_id == Record.id)
        .where(Favorite.user_id == user_id, Record.status == "ACTIVE")
    )
    return list(result.scalars().all())
