from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Self
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import decide_access
from app.database.models import (
    AccessMode,
    GroupMember,
    Permission,
    PermissionCode,
    RoleCode,
    RolePermission,
    User,
    UserPermission,
    UserStatus,
)
from app.services.permissions.cache import get_cached, set_cached
from app.services.permissions.errors import DenyReason


@dataclass(frozen=True)
class UserContext:
    """Права пользователя, не зависящие от конкретного объекта."""

    user_id: UUID
    role: RoleCode
    role_permissions: frozenset[str]
    # Индивидуальные переопределения: True — выдано, False — отозвано
    overrides: dict[str, bool]
    group_ids: frozenset[UUID]

    @property
    def is_superadmin(self) -> bool:
        return self.role == RoleCode.SUPERADMIN

    def has_permission(self, action: PermissionCode) -> bool:
        override = self.overrides.get(action.value)
        if override is not None:
            return override
        return action.value in self.role_permissions

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": str(self.user_id),
            "role": self.role.value,
            "role_permissions": sorted(self.role_permissions),
            "overrides": dict(self.overrides),
            "group_ids": sorted(str(group_id) for group_id in self.group_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            user_id=UUID(data["user_id"]),
            role=RoleCode(data["role"]),
            role_permissions=frozenset(data["role_permissions"]),
            overrides={str(code): bool(granted) for code, granted in data["overrides"].items()},
            group_ids=frozenset(UUID(group_id) for group_id in data["group_ids"]),
        )


def as_uuid(value: UUID | str | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


async def _load_context(session: AsyncSession, user: User) -> UserContext:
    role_result = await session.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == user.role_id)
    )
    override_result = await session.execute(
        select(Permission.code, UserPermission.granted)
        .join(Permission, UserPermission.permission_id == Permission.id)
        .where(UserPermission.user_id == user.id)
    )
    group_result = await session.execute(
        select(GroupMember.group_id).where(GroupMember.user_id == user.id)
    )
    return UserContext(
        user_id=user.id,
        role=RoleCode(user.role),
        role_permissions=frozenset(role_result.scalars().all()),
        overrides={code: bool(granted) for code, granted in override_result.all()},
        group_ids=frozenset(group_result.scalars().all()),
    )


async def get_context(user: User, session: AsyncSession) -> UserContext:
    cached = await get_cached(user.id, session)
    if cached is not None:
        return UserContext.from_dict(cached)
    context = await _load_context(session, user)
    await set_cached(user.id, context.to_dict(), session)
    return context


async def load_access_context(
    user: User | None, session: AsyncSession
) -> tuple[UserContext | None, DenyReason | None]:
    """Проверяет статус пользователя на каждом запросе и возвращает контекст прав."""
    if user is None:
        return None, DenyReason.USER_NOT_FOUND
    # Статус не кешируется: блокировка должна действовать немедленно
    if user.status == UserStatus.BLOCKED:
        return None, DenyReason.USER_BLOCKED
    if user.status != UserStatus.ACTIVE:
        return None, DenyReason.USER_NOT_ACTIVE
    return await get_context(user, session), None


async def load_grants(
    session: AsyncSession,
    model: type,
    owner_column: str,
    owner_id: UUID,
    action: PermissionCode,
    key_column: str,
    keys: Iterable[UUID] | None = None,
) -> dict[UUID, bool]:
    """Возвращает явные записи права (granted) для объекта по ключу group_id или user_id."""
    key = getattr(model, key_column)
    stmt = (
        select(key, model.granted)
        .join(Permission, model.permission_id == Permission.id)
        .where(getattr(model, owner_column) == owner_id, Permission.code == action.value)
    )
    if keys is not None:
        stmt = stmt.where(key.in_(list(keys)))
    result = await session.execute(stmt)
    return {row[0]: bool(row[1]) for row in result.all()}


async def check_scope(
    session: AsyncSession,
    ctx: UserContext,
    mode: AccessMode,
    action: PermissionCode,
    *,
    group_model: type,
    user_model: type,
    owner_column: str,
    owner_id: UUID,
    is_record_owner: bool = False,
) -> bool:
    """Проверяет режим доступа объекта. Правила решает decide_access (Default Deny)."""
    user_grants = await load_grants(
        session, user_model, owner_column, owner_id, action, "user_id", keys=[ctx.user_id]
    )
    explicit = user_grants.get(ctx.user_id)

    group_grants: dict[UUID, bool] = {}
    if ctx.group_ids:
        group_grants = await load_grants(
            session, group_model, owner_column, owner_id, action, "group_id", keys=ctx.group_ids
        )

    decision = decide_access(
        access_mode=mode,
        user_role=ctx.role,
        user_status=UserStatus.ACTIVE,
        is_superadmin=ctx.is_superadmin,
        is_member_of_group=any(group_grants.get(group_id) is True for group_id in ctx.group_ids),
        has_individual_grant=explicit is True,
        # Явный запрет пользователя или группы перекрывает любое разрешение
        has_individual_deny=explicit is False
        or any(group_grants.get(group_id) is False for group_id in ctx.group_ids),
        granted_by_role=ctx.has_permission(action),
        is_record_owner=is_record_owner,
    )
    return bool(decision)
