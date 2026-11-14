from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import GroupMember, RoleCode, User, UserStatus


async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def is_user_active(user: User | None) -> bool:
    return user is not None and user.status == UserStatus.ACTIVE


async def user_can_access_category(session: AsyncSession, user: User, category_code: str) -> bool:
    """Default Deny. SUPERADMIN всегда имеет доступ."""
    if user.role == RoleCode.SUPERADMIN:
        return True

    # Простая проверка по группам (расширяется позже)
    result = await session.execute(
        select(GroupMember).where(GroupMember.user_id == user.id)
    )
    memberships = result.scalars().all()
    group_codes = {m.group.code for m in memberships if m.group}

    mapping = {
        "holodka": "GROUP_HOLODKA",
        "second_stage": "GROUP_SECOND_STAGE",
        "closers": "GROUP_CLOSERS",
        "general": "GROUP_GENERAL",
    }
    required = mapping.get(category_code)
    return required in group_codes if required else False


async def log_audit(
    session: AsyncSession,
    actor_id: UUID | None,
    action: str,
    object_type: str,
    object_id: str | None = None,
    result: str = "SUCCESS",
    metadata: dict | None = None,
) -> None:
    from app.database.models import AuditLog

    log = AuditLog(
        actor_user_id=actor_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        result=result,
        metadata_=metadata,
    )
    session.add(log)
    await session.commit()
