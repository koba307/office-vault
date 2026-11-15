from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AccessMode,
    Category,
    CategoryGroupPermission,
    CategoryPermission,
    CategoryUserPermission,
    PermissionCode,
    User,
)
from app.services.permissions.context import (
    UserContext,
    as_uuid,
    check_scope,
    load_access_context,
)
from app.services.permissions.errors import DenyReason

CATEGORY_ACTIONS = (
    PermissionCode.VIEW,
    PermissionCode.CREATE,
    PermissionCode.EDIT,
    PermissionCode.DELETE,
    PermissionCode.ARCHIVE,
    PermissionCode.RESTORE,
    PermissionCode.DOWNLOAD,
    PermissionCode.UPLOAD,
    PermissionCode.EXPORT,
    PermissionCode.IMPORT,
)

# Защита от циклов в дереве категорий
MAX_CATEGORY_DEPTH = 16


async def resolve_category_id(session: AsyncSession, value: UUID | str | None) -> UUID | None:
    """Принимает UUID или код категории и возвращает UUID."""
    if value is None:
        return None
    category_id = as_uuid(value)
    if category_id is not None:
        return category_id
    result = await session.execute(select(Category.id).where(Category.code == str(value)))
    return result.scalar_one_or_none()


async def resolve_scope(session: AsyncSession, category_id: UUID) -> tuple[UUID, AccessMode] | None:
    """Ищет ближайший CategoryPermission вверх по дереву.

    Возвращает (id категории с правилом, режим доступа) или None, если правил нет.
    """
    current: UUID | None = category_id
    for _ in range(MAX_CATEGORY_DEPTH):
        if current is None:
            return None
        result = await session.execute(
            select(Category.parent_id, CategoryPermission.access_mode)
            .select_from(Category)
            .outerjoin(CategoryPermission, CategoryPermission.category_id == Category.id)
            .where(Category.id == current)
        )
        row = result.one_or_none()
        if row is None:
            return None
        parent_id, mode = row
        if mode is not None:
            return current, mode
        current = parent_id
    return None


async def category_denial_reason(
    ctx: UserContext,
    category_id: UUID,
    action: PermissionCode,
    session: AsyncSession,
) -> DenyReason | None:
    """Возвращает причину отказа по категории или None, если доступ разрешён."""
    category = await session.get(Category, category_id)
    if category is None:
        return DenyReason.CATEGORY_NOT_FOUND
    if not ctx.has_permission(action):
        return DenyReason.ROLE_DENIED
    if ctx.is_superadmin:
        return None
    if category.status != "ACTIVE":
        return DenyReason.CATEGORY_DENIED

    scope = await resolve_scope(session, category_id)
    if scope is None:
        return DenyReason.CATEGORY_DENIED
    owner_id, mode = scope
    allowed = await check_scope(
        session,
        ctx,
        mode,
        action,
        group_model=CategoryGroupPermission,
        user_model=CategoryUserPermission,
        owner_column="category_id",
        owner_id=owner_id,
    )
    return None if allowed else DenyReason.CATEGORY_DENIED


async def check_category_access(
    user: User | None, category_code: str, session: AsyncSession
) -> bool:
    ctx, reason = await load_access_context(user, session)
    if reason is not None or ctx is None:
        return False
    category_id = await resolve_category_id(session, category_code)
    if category_id is None:
        return False
    return await category_denial_reason(ctx, category_id, PermissionCode.VIEW, session) is None


async def check_category_permission(
    user: User | None,
    category_id: UUID | str,
    action: PermissionCode | str,
    session: AsyncSession,
) -> bool:
    ctx, reason = await load_access_context(user, session)
    if reason is not None or ctx is None:
        return False
    resolved = await resolve_category_id(session, category_id)
    if resolved is None:
        return False
    return await category_denial_reason(ctx, resolved, PermissionCode(action), session) is None
