from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Group, GroupMember, Role, RoleCode, User, UserStatus


async def list_users(session: AsyncSession, limit: int = 50) -> list[User]:
    result = await session.execute(select(User).order_by(User.created_at.desc()).limit(limit))
    return list(result.scalars().all())


async def get_user_by_id(session: AsyncSession, user_id: UUID) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def set_user_role(session: AsyncSession, user_id: UUID, role: RoleCode) -> None:
    if role == RoleCode.SUPERADMIN:
        raise ValueError("SUPERADMIN can only be assigned through server configuration")
    user = await get_user_by_id(session, user_id)
    if user:
        if user.role == RoleCode.SUPERADMIN:
            raise ValueError("The configured SUPERADMIN role cannot be changed through the UI")
        result = await session.execute(select(Role).where(Role.code == role.value))
        role_record = result.scalar_one_or_none()
        if role_record is None:
            raise ValueError(f"Role {role.value} does not exist")
        user.role = role
        user.role_id = role_record.id
        await session.commit()


async def set_user_status(session: AsyncSession, user_id: UUID, status: UserStatus) -> None:
    user = await get_user_by_id(session, user_id)
    if user:
        if user.role == RoleCode.SUPERADMIN and status != UserStatus.ACTIVE:
            raise ValueError("The configured SUPERADMIN cannot be blocked through the UI")
        user.status = status
        await session.commit()


async def list_groups(session: AsyncSession) -> list[Group]:
    result = await session.execute(select(Group).order_by(Group.name))
    return list(result.scalars().all())


async def get_user_groups(session: AsyncSession, user_id: UUID) -> list[Group]:
    result = await session.execute(
        select(Group)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .where(GroupMember.user_id == user_id)
    )
    return list(result.scalars().all())


async def assign_user_to_group(session: AsyncSession, user_id: UUID, group_id: UUID, assigned_by: UUID) -> None:
    exists = await session.execute(
        select(GroupMember).where(GroupMember.user_id == user_id, GroupMember.group_id == group_id)
    )
    if exists.scalar_one_or_none():
        return
    member = GroupMember(user_id=user_id, group_id=group_id, assigned_by=assigned_by)
    session.add(member)
    await session.commit()


async def remove_user_from_group(session: AsyncSession, user_id: UUID, group_id: UUID) -> None:
    result = await session.execute(
        select(GroupMember).where(GroupMember.user_id == user_id, GroupMember.group_id == group_id)
    )
    member = result.scalar_one_or_none()
    if member:
        await session.delete(member)
        await session.commit()
