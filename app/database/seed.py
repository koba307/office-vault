import logging
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database.models import (
    AccessMode,
    Category,
    CategoryGroupPermission,
    CategoryPermission,
    CategoryRole,
    Group,
    Permission,
    PermissionCode,
    Record,
    RecordType,
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
    RoleCode.COACH.value: [p for p, _ in PERMISSIONS],
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

    # Подкатегория MAX внутри «2й Этап» (выдача MAX-аккаунтов).
    max_category = categories_by_code.get("max")
    if max_category is None:
        second_stage = categories_by_code["second_stage"]
        max_category = Category(
            code="max",
            name="MAX",
            parent_id=second_stage.id,
            category_type="subcategory",
            sort_order=90,
            status="ACTIVE",
        )
        session.add(max_category)
        await session.flush()
        categories_by_code["max"] = max_category

    result = await session.execute(select(CategoryPermission))
    category_permissions = {entry.category_id: entry for entry in result.scalars().all()}
    for code, _, _ in CATEGORIES:
        category = categories_by_code[code]
        permission = category_permissions.get(category.id)
        if permission is None:
            session.add(
                CategoryPermission(category_id=category.id, access_mode=AccessMode.GROUP)
            )
    max_permission = category_permissions.get(max_category.id)
    if max_permission is None:
        session.add(
            CategoryPermission(
                category_id=max_category.id, access_mode=AccessMode.ALL_EMPLOYEES
            )
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



BOT_ITEMS = {
    "bots_data": [
        (2, "@itb_inform_r4_bot", "«Информатор» — поиск данных, заявки и выдача доступа"),
        (3, "@saur_priv_r3_bot", "SAURON API Search Bot — платный поиск данных через API"),
        (5, "@kodysu_checker_v2_bot", "Проверка номеров телефонов через kody.su: оператор, регион"),
        (7, "@SangMata_BOT", "История имён и юзернеймов пользователей"),
        (13, "@ins_lo_v2_bot", "Выгрузка открытых Instagram-профилей в ZIP"),
        (0, "SAURON Сайт", "Ссылка\n<code>http://sauron.info</code>\nЛогин\n<code>admin_it_dp</code>\nПароль\n<code>QhXqvANrebkygjan</code>"),
    ],
    "bots_misc": [
        (4, "@gruzhki_bot", "Создание видеокружков из обычных видео"),
        (6, "@Getmyid_bot", "Получение Telegram ID пользователя или чата"),
        (8, "@tg_media_export_bot", "Выгрузка медиа из закрытых каналов/групп, сборка EXE"),
    ],
    "bots_second_stage": [
        (11, "@gosuslugi_sequre_v2_bot", "Госуслуги Support — защищённый канал поддержки портала"),
        (10, "@skriny_overlay_v2_bot", "Вставка скриншота панели в шаблон (оверлей)"),
    ],
    "bots_closers": [
        (1, "@mostbot2_bot", "Сбор данных с Android-устройств (ScanDocs): контакты, SMS, звонки, файлы"),
    ],
    "bots_superadmin": [
        (12, "@up_mob_v2_bot", "Пополнение мобильных счетов, проверка статуса платежей"),
    ],
}

def _proxy_link(url: str) -> str:
    safe = url.replace("&", "&amp;")
    return f'<a href="{safe}">{url}</a>'


VPN_ITEMS = [
    (
        "VPN аккаунты",
        (
            "Аккаунт 1\n<code>newwwer5@gmail.com</code>\n<code>Work9889!</code>\n\n"
            "Аккаунт 2\n<code>meduzavkisile@gmail.com</code>\n<code>83vaYLXq</code>\n\n"
            "Аккаунт 3\n<code>newfree9889@gmail.com</code>\n<code>Work9889!</code>"
        ),
    ),
    (
        "Прокси",
        "SOCKS5 RU (144.172.119.34)\n"
        + "\n".join(_proxy_link(f"tg://socks?server=144.172.119.34&port={p}&user=maxchrome&pass=wdeFLIfBaAjMhyfO") for p in (31849, 31850, 31851, 31852, 31853, 31854))
        + "\n\nSOCKS5 DE\n"
        + _proxy_link("tg://socks?server=144.172.119.34&port=31847&user=maxchrome&pass=wdeFLIfBaAjMhyfO")
        + "\n\nSOCKS5 msk-02 (Москва, gost)\n"
        + _proxy_link("tg://socks?server=45.86.79.168&port=1080&user=vps-main&pass=9qauhSESP9RKF0wELTY7P6b4lTaPMK69")
        + "\n"
        + _proxy_link("tg://socks?server=83.141.92.249&port=1080&user=vps-249&pass=XdyKkXiRMnQnbNfj7LHXF3FrddZcboRv")
        + "\n\nMTProto Germany\n"
        + _proxy_link("tg://proxy?server=144.172.119.34&port=8443&secret=eedea5aa19ff1962e3d1de2f80f9e76eb07777772e6d6963726f736f66742e636f6d")
        + "\n\nMTProto Frankfurt\n"
        + _proxy_link("tg://proxy?server=89.169.52.232&port=8443&secret=7mskmZa_hkSzNeLHEss0m1xtaWNyb3NvZnQuY29t")
        + "\n\nMTProto Москва\n"
        + _proxy_link("tg://proxy?server=171.22.129.133&port=8443&secret=7pTmK58_vicp11bHpZwwN_ZtaWNyb3NvZnQuY29t"),
    ),
]

BOT_CATEGORY_DEFS = [
    ("bots", "Боты", None),
    ("vpn", "VPN", "general"),
    ("bots_data", "Пробивы/Данные", "bots"),
    ("bots_misc", "Разные", "bots"),
    ("bots_second_stage", "2 этап", "bots"),
    ("bots_closers", "Клоузеры", "bots"),
    ("bots_superadmin", "Суперадмин", "bots"),
]

BOT_CATEGORY_ROLES = {
    "max": ["second_stage"],
    "bots_second_stage": ["second_stage", "COACH"],
    "bots_closers": ["closers", "COACH"],
}


async def _seed_bot_directory(session: AsyncSession) -> None:
    """Создаёт справочник ботов OFFICE VAULT (groups/subgroups + links)."""
    superadmin = (
        await session.execute(select(User).where(User.role == RoleCode.SUPERADMIN).limit(1))
    ).scalar_one_or_none()
    if superadmin is None:
        return
    categories = {c.code: c for c in (await session.execute(select(Category))).scalars().all()}
    for code, name, parent_code in BOT_CATEGORY_DEFS:
        if code in categories:
            continue
        parent = categories.get(parent_code) if parent_code else None
        cat = Category(
            code=code,
            name=name,
            parent_id=parent.id if parent else None,
            category_type="subcategory" if parent else "root",
            sort_order=100,
            status="ACTIVE",
        )
        session.add(cat)
        await session.flush()
        categories[code] = cat

    # Общие разделы «Боты» и «VPN» доступны всем активным сотрудникам.
    existing_cp = {
        cp.category_id
        for cp in (await session.execute(select(CategoryPermission))).scalars().all()
    }
    for code in ("bots", "bots_data", "bots_misc", "vpn"):
        cat = categories.get(code)
        if cat is not None and cat.id not in existing_cp:
            session.add(
                CategoryPermission(
                    category_id=cat.id, access_mode=AccessMode.ALL_EMPLOYEES
                )
            )
            existing_cp.add(cat.id)

    # MAX доступен только роли «2 этап».
    max_cat = categories.get("max")
    if max_cat is not None:
        pass

    # Общие подгруппы ботов не должны иметь.role-ограничений.
    for code in ("bots_data", "bots_misc"):
        cat = categories.get(code)
        if cat is not None:
            await session.execute(
                __import__("sqlalchemy").delete(CategoryRole).where(
                    CategoryRole.category_id == cat.id
                )
            )

    roles = {r.code: r for r in (await session.execute(select(Role))).scalars().all()}
    existing_cr = {
        (r.category_id, r.role_id)
        for r in (await session.execute(select(CategoryRole))).scalars().all()
    }
    for cat_code, role_codes in BOT_CATEGORY_ROLES.items():
        cat = categories.get(cat_code)
        if cat is None:
            continue
        for role_code in role_codes:
            role = roles.get(role_code)
            if role is None:
                continue
            if (cat.id, role.id) in existing_cr:
                continue
            session.add(CategoryRole(category_id=cat.id, role_id=role.id))
            existing_cr.add((cat.id, role.id))

    existing_titles = set((await session.execute(select(Record.title))).scalars().all())
    for cat_code, items in BOT_ITEMS.items():
        cat = categories.get(cat_code)
        if cat is None:
            continue
        for num, username, description in items:
            title = f"{num}. {username}" if num else username
            if title in existing_titles:
                continue
            session.add(
                Record(
                    id=uuid4(),
                    category_id=cat.id,
                    record_type=RecordType.LINKS,
                    title=title,
                    description=description,
                    status="ACTIVE",
                    created_by=superadmin.id,
                )
            )
            existing_titles.add(title)
    vpn_cat = categories.get("vpn")
    proxies_cat = categories.get("proxies")
    if proxies_cat is None and vpn_cat is not None:
        proxies_cat = Category(
            code="proxies",
            name="Прокси",
            parent_id=vpn_cat.id,
            category_type="subcategory",
            sort_order=101,
            status="ACTIVE",
        )
        session.add(proxies_cat)
        await session.flush()
        categories["proxies"] = proxies_cat
        if proxies_cat.id not in existing_cp:
            session.add(
                CategoryPermission(category_id=proxies_cat.id, access_mode=AccessMode.ALL_EMPLOYEES)
            )
            existing_cp.add(proxies_cat.id)
    for title, description in VPN_ITEMS:
        target = proxies_cat if title == "Прокси" else vpn_cat
        if target is None or title in existing_titles:
            continue
        session.add(
            Record(
                id=uuid4(),
                category_id=target.id,
                record_type=RecordType.CREDENTIALS,
                title=title,
                description=description,
                status="ACTIVE",
                created_by=superadmin.id,
            )
        )
        existing_titles.add(title)
    await session.commit()

async def seed_system_data(session: AsyncSession) -> None:
    permissions = await _seed_permissions(session)
    await _seed_roles(session, permissions)
    await _seed_categories_and_groups(session)

    # Динамические роли, соответствующие рабочим разделам (RBAC).
    role_defs = [
        ("holodka", "Холодка"),
        ("second_stage", "2й Этап"),
        ("closers", "Клоузеры"),
    ]
    roles_by_code = {r.code: r for r in (await session.execute(select(Role))).scalars().all()}
    for code, name in role_defs:
        if code not in roles_by_code:
            role = Role(code=code, name=name, is_system=True)
            session.add(role)
            await session.flush()
            roles_by_code[code] = role
    # Связь категорий с ролями (Общие доступна всем, роли не требуются).
    categories = {c.code: c for c in (await session.execute(select(Category))).scalars().all()}
    category_role_pairs = [
        ("holodka", "holodka"),
        ("second_stage", "second_stage"),
        ("closers", "closers"),
    ]
    existing = {(cr.category_id, cr.role_id) for cr in (await session.execute(select(CategoryRole))).scalars().all()}
    for cat_code, role_code in category_role_pairs:
        cat = categories.get(cat_code)
        role = roles_by_code.get(role_code)
        if cat is None or role is None:
            continue
        if (cat.id, role.id) in existing:
            continue
        session.add(CategoryRole(category_id=cat.id, role_id=role.id))
        existing.add((cat.id, role.id))

    # Backfill role_id for existing users
    result = await session.execute(select(User))
    users = result.scalars().all()
    for user in users:
        await _link_user_role(session, user)
    await _seed_bot_directory(session)

    await session.commit()
    logger.info("System data seeded: roles, permissions, categories, groups, SUPERADMIN")


__all__ = ["seed_system_data"]
