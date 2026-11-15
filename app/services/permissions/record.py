from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    PermissionCode,
    Record,
    RecordGroupPermission,
    RecordPermission,
    RecordUserPermission,
    User,
)
from app.services.permissions.category import category_denial_reason
from app.services.permissions.context import (
    UserContext,
    as_uuid,
    check_scope,
    load_access_context,
)
from app.services.permissions.errors import DenyReason


async def record_denial_reason(
    ctx: UserContext,
    record_id: UUID,
    action: PermissionCode,
    session: AsyncSession,
) -> DenyReason | None:
    """Возвращает причину отказа по записи или None, если доступ разрешён.

    Запись должна быть доступна и через категорию, и через собственный RecordPermission.
    Если RecordPermission нет, решение целиком наследуется от категории.
    """
    record = await session.get(Record, record_id)
    if record is None:
        return DenyReason.RECORD_NOT_FOUND
    if record.status == "DELETED":
        return DenyReason.RECORD_DELETED
    if not ctx.has_permission(action):
        return DenyReason.ROLE_DENIED

    # Категория и подкатегория — внешний барьер, его нельзя обойти записью
    reason = await category_denial_reason(ctx, record.category_id, action, session)
    if reason is not None:
        return reason
    if record.subcategory_id is not None:
        reason = await category_denial_reason(ctx, record.subcategory_id, action, session)
        if reason is not None:
            return reason

    if ctx.is_superadmin:
        return None
    result = await session.execute(
        select(RecordPermission.access_mode).where(RecordPermission.record_id == record_id)
    )
    mode = result.scalar_one_or_none()
    if mode is None:
        return None

    allowed = await check_scope(
        session,
        ctx,
        mode,
        action,
        group_model=RecordGroupPermission,
        user_model=RecordUserPermission,
        owner_column="record_id",
        owner_id=record_id,
        is_record_owner=record.created_by == ctx.user_id,
    )
    return None if allowed else DenyReason.RECORD_DENIED


async def check_record_access(
    user: User | None, record_id: UUID | str, session: AsyncSession
) -> bool:
    return await check_record_permission(user, record_id, PermissionCode.VIEW, session)


async def check_record_permission(
    user: User | None,
    record_id: UUID | str,
    action: PermissionCode | str,
    session: AsyncSession,
) -> bool:
    ctx, reason = await load_access_context(user, session)
    if reason is not None or ctx is None:
        return False
    resolved = as_uuid(record_id)
    if resolved is None:
        return False
    return await record_denial_reason(ctx, resolved, PermissionCode(action), session) is None
