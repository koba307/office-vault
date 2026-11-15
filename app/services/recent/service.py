"""История недавних просмотров (Phase 07).

В истории хранятся только связь пользователь↔запись и дата просмотра:
значения полей, раскрытые секреты и шифртексты сюда не попадают никогда.
Список при каждой выдаче собирается заново и фильтруется по актуальным
правам (Default Deny): если доступ изменился, запись исключается из выдачи.
"""

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Category, RecentRecord, Record, User
from app.services.permissions import PermissionService

# История конечна: держим не более N последних просмотров на пользователя
RECENT_HISTORY_LIMIT = 200
# Размер страницы выдачи по умолчанию
RECENT_DEFAULT_LIMIT = 10


@dataclass(frozen=True)
class RecentEntry:
    """Строка истории: только название, категория, тип и дата просмотра."""

    record_id: UUID
    title: str
    category_name: str
    record_type: str
    viewed_at: datetime | None


def _type_value(record_type) -> str:
    """Нормализует значение Enum/строки типа записи в строку."""
    if isinstance(record_type, enum.Enum):
        return record_type.value
    return str(record_type)


async def _trim_recent(session: AsyncSession, user_id: UUID) -> None:
    """Удаляет просмотры сверх лимита, оставляя самые свежие."""
    stale = (
        await session.execute(
            select(RecentRecord.record_id)
            .where(RecentRecord.user_id == user_id)
            .order_by(RecentRecord.viewed_at.desc())
            .offset(RECENT_HISTORY_LIMIT)
        )
    ).scalars().all()
    if stale:
        await session.execute(
            delete(RecentRecord).where(
                RecentRecord.user_id == user_id,
                RecentRecord.record_id.in_(list(stale)),
            )
        )


async def add_recent(session: AsyncSession, user_id: UUID, record_id: UUID) -> None:
    """Фиксирует просмотр записи: обновляет viewed_at или добавляет новую строку.

    Вызывается только после проверки прав на просмотр (см. record:view).
    В историю попадает лишь факт просмотра — без значений полей и секретов.
    """
    now = datetime.now(UTC)
    entry = (
        await session.execute(
            select(RecentRecord).where(
                RecentRecord.user_id == user_id,
                RecentRecord.record_id == record_id,
            )
        )
    ).scalar_one_or_none()
    if entry is not None:
        entry.viewed_at = now
    else:
        session.add(RecentRecord(user_id=user_id, record_id=record_id, viewed_at=now))
    await _trim_recent(session, user_id)
    await session.commit()


async def list_recent(
    session: AsyncSession,
    user_id: UUID,
    *,
    limit: int = RECENT_DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[RecentEntry], int]:
    """Страница истории и общее число доступных просмотров (для пагинации).

    Проверка актуальности доступа выполняется при выдаче: архивные, удалённые
    и ставшие недоступными записи исключаются из списка.
    """
    user = await session.get(User, user_id)
    if user is None:
        return [], 0

    rows = (
        await session.execute(
            select(
                RecentRecord.record_id,
                RecentRecord.viewed_at,
                Record.title,
                Category.name,
                Record.record_type,
            )
            .join(Record, Record.id == RecentRecord.record_id)
            .join(Category, Category.id == Record.category_id)
            .where(
                RecentRecord.user_id == user_id,
                Record.status == "ACTIVE",
                Category.status == "ACTIVE",
            )
            .order_by(RecentRecord.viewed_at.desc(), RecentRecord.record_id)
        )
    ).all()

    visible: list[RecentEntry] = []
    for record_id, viewed_at, title, category_name, record_type in rows:
        # Default Deny: права могли измениться после просмотра — проверяем заново
        if not await PermissionService.can(user, "VIEW", "record", record_id, session):
            continue
        visible.append(
            RecentEntry(
                record_id=record_id,
                title=title,
                category_name=category_name,
                record_type=_type_value(record_type),
                viewed_at=viewed_at,
            )
        )

    limit = max(int(limit), 1)
    offset = max(int(offset), 0)
    return visible[offset : offset + limit], len(visible)


async def clear_recent(session: AsyncSession, user_id: UUID) -> int:
    """Полностью очищает историю просмотров пользователя. Возвращает число удалённых строк."""
    result = await session.execute(delete(RecentRecord).where(RecentRecord.user_id == user_id))
    await session.commit()
    return int(result.rowcount or 0)
