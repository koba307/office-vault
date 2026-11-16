"""Сервис аудита: запись и выборка событий."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AuditLog


async def write_audit(
    session: AsyncSession,
    *,
    user_id: UUID | None,
    action: str,
    object_type: str,
    object_id: str | None = None,
    result: str = "SUCCESS",
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    """Записывает событие аудита и возвращает созданную запись."""
    log = AuditLog(
        id=uuid4(),
        user_id=user_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        result=result,
        metadata=metadata or {},
        created_at=datetime.now(UTC),
    )
    session.add(log)
    await session.commit()
    return log


async def list_audit(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    user_id: UUID | None = None,
    action: str | None = None,
    object_type: str | None = None,
) -> list[AuditLog]:
    """Возвращает последние события аудита с простой фильтрацией."""
    query = select(AuditLog).order_by(AuditLog.created_at.desc())
    if user_id is not None:
        query = query.where(AuditLog.user_id == user_id)
    if action:
        query = query.where(AuditLog.action == action)
    if object_type:
        query = query.where(AuditLog.object_type == object_type)
    query = query.offset(offset).limit(limit)
    result = await session.execute(query)
    return list(result.scalars().all())


async def list_security_events(session: AsyncSession, *, limit: int = 50) -> list:
    """Возвращает последние события безопасности."""
    from app.database.models import SecurityEvent

    result = await session.execute(
        select(SecurityEvent).order_by(SecurityEvent.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())
