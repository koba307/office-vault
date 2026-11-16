import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database.models import (
    AccessMode,
    Category,
    CategoryGroupPermission,
    CategoryPermission,
    Group,
    Permission,
    PermissionCode,
    Role,
    RoleCode,
    RolePermission,
    User,
    UserStatus,
)

logger = logging.getLogger(__name__)

PERMISSIONS = [
    (PermissionCode.VIEW, "Просмотр"),
    (PermissionCode.CREATE, "Создание"),
    (PermissionCode.EDIT, "Редактирование"),
    (PermissionCode.DELETE, "Удаление"),
    (PermissionCode.ARCHIVE, "Архивирование"),
    (PermissionCode.RESTORE, "Восстановление"),
    (PermissionCode.DOWNLOAD, "Скачивание"),
    (PermissionCode.UPLOAD, "Загрузка"),
    (PermissionCode.EXPORT, "Экспорт"),
    (PermissionCode.EXPORT_SECRETS, "Экспорт секретов"),
    (PermissionCode.IMPORT, "Импорт"),
    (PermissionCode.MANAGE_ACCESS, "Управление доступом"),
    (PermissionCode.MANAGE_USERS, "Управление пользователями"),
    (PermissionCode.MANAGE_GROUPS, "Управление группами"),
    (PermissionCode.MANAGE_CATEGORIES, "Управление категориями"),
    (PermissionCode.VIEW_AUDIT, "Просмотр аудита"),
    (PermissionCode.MANAGE_BACKUP, "Управление резервными копиями"),
    (PermissionCode.MANAGE_SYSTEM, "Управление системой"),
]

ROLE_PERMISSIONS: dict[str, list[PermissionCode]] = {
    RoleCode.SUPERADMIN.value: [p for p, _ in PERMISSIONS],
    RoleCode.BOSS.value: [
        PermissionCode.VIEW,
        PermissionCode.CREATE,
        PermissionCode.EDIT,
        PermissionCode.DELETE,
        PermissionCode.ARCHIVE,
        PermissionCode.RESTORE,
        PermissionCode.DOWNLOAD,
        PermissionCode.UPLOAD,
        PermissionCode.EXPORT,
        PermissionCode.EXPORT_SECRETS,
        PermissionCode.IMPORT,
        PermissionCode.MANAGE_ACCESS,
        PermissionCode.MANAGE_CATEGORIES,
        PermissionCode.VIEW_AUDIT,
    ],
    RoleCode.EMPLOYEE.value: [PermissionCode.VIEW, PermissionCode.DOWNLOAD],
}

CATEGORIES = [
    ("holodka", "Холодка", 1),
    ("second_stage", "2й Этап", 2),
    ("closers", "Клоузеры", 3),
    ("general", "Общее", 4),
]

GROUPS = [
    ("GROUP_HOLODKA", "Холодка"),
    ("GROUP_SECOND_STAGE", "2й Этап"),
    ("GROUP_CLOSERS", "Клоузеры"),
    ("GROUP_GENERAL", "Общее"),
]


async def _seed_permissions(session: AsyncSession) -> dict[str, Permission]:
    result = await session.execute(select(Permission))
    by_code = {p.code: p for p in result.scalars().all()}
    for code, name in PERMISSIONS:
        if code.value not in by_code:
            permission = Permission(code=code.value, name=name)
            session.add(permission)
            by_code[code.value] = permission
    await session.flush()
    return by_code


async def _seed_roles(session: AsyncSession, permissions: dict[str, Permission]) -> None:
    result = await session.execute(select(Role))
    by_code = {r.code: r for r in result.scalars().all()}
    for code in RoleCode:
        if code.value not in by_code:
            role = Role(code=code.value, name=code.value, is_system=True)
            session.add(role)
            by_code[code.value] = role
    await session.flush()

    result = await session.execute(select(RolePermission.role_id, RolePermission.permission_id))
    existing = {(row.role_id, row.permission_id) for row in result.all()}
    for role_code, allowed in ROLE_PERMISSIONS.items():
        role = by_code[role_code]
        for code in allowed:
            permission = permissions.get(code.value)
            pair = (role.id, permission.id) if permission else None
            if permission is None or pair in existing:
                continue
            session.add(RolePermission(role_id=role.id, permission_id=permission.id))
            existing.add(pair)


async def _seed_categories_and_groups(session: AsyncSession) -> None:
    result = await session.execute(select(Category))
    categories_by_code = {category.code: category for category in result.scalars().all()}
    for code, name, sort_order in CATEGORIES:
        category = categories_by_code.get(code)
        if category is None:
            category = Category(
                code=code, name=name, category_type="root", sort_order=sort_order, status="ACTIVE"
            )
            session.add(category)
            categories_by_code[code] = category
        elif category.name != name or category.category_type != "root":
            raise RuntimeError(
                f"System category {code!r} must remain {name!r} with type 'root'; "
                "seed will not rename or reclassify existing categories automatically."
            )
    await session.flush()

    result = await session.execute(select(CategoryPermission))
    category_permissions = {entry.category_id: entry for entry in result.scalars().all()}
    for code, _, _ in CATEGORIES:
        category = categories_by_code[code]
        permission = category_permissions.get(category.id)
        if permission is None:
            session.add(
                CategoryPermission(category_id=category.id, access_mode=AccessMode.GROUP)
            )

    result = await session.execute(select(Group))
    groups_by_code = {group.code: group for group in result.scalars().all()}
    for code, name in GROUPS:
        group = groups_by_code.get(code)
        if group is None:
            group = Group(code=code, name=name, is_system=True)
            session.add(group)
            groups_by_code[code] = group
        else:
            if group.name != name:
                raise RuntimeError(
                    f"System group {code!r} must retain its configured name {name!r}; "
                    "seed will not rename it automatically."
                )
            group.is_system = True
    await session.flush()

    # Grant VIEW to each group on its own category
    view_permission = next(
        permission for permission, _ in PERMISSIONS if permission == PermissionCode.VIEW
    )
    result = await session.execute(
        select(Permission).where(Permission.code == view_permission.value)
    )
    view_permission_row = result.scalar_one()

    result = await session.execute(
        select(
            CategoryGroupPermission.category_id,
            CategoryGroupPermission.group_id,
            CategoryGroupPermission.permission_id,
        )
    )
    existing_category_group_permissions = {
        (row.category_id, row.group_id, row.permission_id) for row in result.all()
    }
    category_group_codes = [
        ("holodka", "GROUP_HOLODKA"),
        ("second_stage", "GROUP_SECOND_STAGE"),
        ("closers", "GROUP_CLOSERS"),
        ("general", "GROUP_GENERAL"),
    ]
    for category_code, group_code in category_group_codes:
        category = categories_by_code[category_code]
        group = groups_by_code[group_code]
        permission_key = (category.id, group.id, view_permission_row.id)
        if permission_key not in existing_category_group_permissions:
            session.add(
                CategoryGroupPermission(
                    category_id=category.id,
                    group_id=group.id,
                    permission_id=view_permission_row.id,
                    granted=True,
                )
            )
            existing_category_group_permissions.add(permission_key)

    # Grant BOSS access to all 4 categories via all groups (Mixed Access)
    boss_permission_codes = [
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
        PermissionCode.MANAGE_ACCESS,
        PermissionCode.MANAGE_CATEGORIES,
        PermissionCode.VIEW_AUDIT,
    ]
    for category_code, _, _ in CATEGORIES:
        category = categories_by_code[category_code]
        for group_code, _ in GROUPS:
            group = groups_by_code[group_code]
            for perm_code in boss_permission_codes:
                perm_result = await session.execute(
                    select(Permission).where(Permission.code == perm_code.value)
                )
                perm = perm_result.scalar_one()
                permission_key = (category.id, group.id, perm.id)
                if permission_key not in existing_category_group_permissions:
                    session.add(
                        CategoryGroupPermission(
                            category_id=category.id,
                            group_id=group.id,
                            permission_id=perm.id,
                            granted=True,
                        )
                    )
                    existing_category_group_permissions.add(permission_key)

    result = await session.execute(select(Role).where(Role.code == RoleCode.SUPERADMIN.value))
    superadmin_role = result.scalar_one()
    result = await session.execute(
        select(User).where(User.telegram_id == settings.superadmin_telegram_id)
    )
    superadmin = result.scalar_one_or_none()
    if superadmin is None:
        superadmin = User(
            telegram_id=settings.superadmin_telegram_id,
            display_name="SUPERADMIN",
            role=RoleCode.SUPERADMIN,
            role_id=superadmin_role.id,
            status=UserStatus.ACTIVE,
        )
        session.add(superadmin)
    else:
        superadmin.role = RoleCode.SUPERADMIN
        superadmin.role_id = superadmin_role.id
        if not superadmin.display_name:
            superadmin.display_name = "SUPERADMIN"
    await session.flush()
    await _link_user_role(session, superadmin)


async def _link_user_role(session: AsyncSession, user: User) -> None:
    result = await session.execute(select(Role).where(Role.code == user.role.value))
    role = result.scalar_one_or_none()
    if role is None:
        raise RuntimeError(f"Role {user.role.value!r} is missing from the system role catalog")
    if user.role_id != role.id:
        user.role_id = role.id


async def seed_system_data(session: AsyncSession) -> None:
    permissions = await _seed_permissions(session)
    await _seed_roles(session, permissions)
    await _seed_categories_and_groups(session)

    # Backfill role_id for existing users
    result = await session.execute(select(User))
    users = result.scalars().all()
    for user in users:
        await _link_user_role(session, user)

    await session.commit()
    logger.info("System data seeded: roles, permissions, categories, groups, SUPERADMIN")


__all__ = ["seed_system_data"]
