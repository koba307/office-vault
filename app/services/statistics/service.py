"""Сводная статистика по записям, файлам, пользователям и категориям."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Category, File, Record, User, UserStatus


async def overall_stats(session: AsyncSession) -> dict[str, int]:
    """Общая статистика: записи, файлы, пользователи, категории."""
    active_records = await session.scalar(
        select(func.count()).select_from(Record).where(Record.status == "ACTIVE")
    ) or 0
    archived_records = await session.scalar(
        select(func.count()).select_from(Record).where(Record.status == "ARCHIVED")
    ) or 0
    deleted_records = await session.scalar(
        select(func.count()).select_from(Record).where(Record.status == "DELETED")
    ) or 0
    files = await session.scalar(
        select(func.count()).select_from(File).where(File.deleted_at.is_(None))
    ) or 0
    active_users = await session.scalar(
        select(func.count()).select_from(User).where(User.status == UserStatus.ACTIVE)
    ) or 0
    blocked_users = await session.scalar(
        select(func.count()).select_from(User).where(User.status == UserStatus.BLOCKED)
    ) or 0
    categories = await session.scalar(
        select(func.count()).select_from(Category).where(Category.status == "ACTIVE")
    ) or 0
    return {
        "active_records": int(active_records),
        "archived_records": int(archived_records),
        "deleted_records": int(deleted_records),
        "files": int(files),
        "active_users": int(active_users),
        "blocked_users": int(blocked_users),
        "categories": int(categories),
    }


async def records_by_type(session: AsyncSession) -> dict[str, int]:
    """Количество активных записей по типам."""
    rows = (
        await session.execute(
            select(Record.record_type, func.count())
            .where(Record.status == "ACTIVE")
            .group_by(Record.record_type)
        )
    ).all()
    return {str(rt.value if hasattr(rt, "value") else rt): int(cnt) for rt, cnt in rows}


async def records_by_category(session: AsyncSession) -> dict[str, int]:
    """Количество активных записей по категориям."""
    rows = (
        await session.execute(
            select(Category.code, func.count(Record.id))
            .join(Record, Record.category_id == Category.id, isouter=True)
            .where(Record.status == "ACTIVE")
            .group_by(Category.code)
        )
    ).all()
    return {str(code): int(cnt or 0) for code, cnt in rows}
