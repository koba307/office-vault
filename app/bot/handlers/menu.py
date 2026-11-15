import html
import re
import time
from dataclasses import dataclass
from uuid import UUID

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AuditLog,
    Backup,
    Category,
    File,
    Group,
    GroupMember,
    PermissionCode,
    Record,
    RecordType,
    RecordVersion,
    RoleCode,
    SecurityEvent,
    SystemSetting,
    User,
    UserStatus,
)
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active
from app.services.categories import get_category, list_categories
from app.services.permissions import PermissionService
from app.services.records.service import get_record, list_records_page

router = Router()

MAIN_TEXT = "🏢 <b>OFFICE VAULT</b>\n\nВыберите рабочий раздел:"
STALE_TEXT = "Кнопка устарела. Откройте меню заново."

PAGE_SIZE = 10

SORT_DATE = "d"
SORT_NAME = "n"
SORT_TITLES = {SORT_DATE: "по дате", SORT_NAME: "по названию"}

ROOT_EMOJI = {
    "holodka": "❄️",
    "second_stage": "2️⃣",
    "closers": "🎯",
    "general": "🌐",
}

# Названия корневых разделов используются как fallback и в тестах длины callback.
CATEGORY_NAMES = {
    "holodka": "❄️ ХОЛОДКА",
    "second_stage": "2️⃣ 2Й ЭТАП",
    "closers": "🎯 КЛОУЗЕРЫ",
    "general": "🌐 ОБЩЕЕ",
}

TYPE_TITLES = {
    RecordType.CREDENTIALS: "🔐 Логины и пароли",
    RecordType.SIP: "☎️ SIP и телефония",
    RecordType.LINKS: "🔗 Ссылки",
    RecordType.SETTINGS: "⚙️ Настройки",
    RecordType.INSTRUCTIONS: "📚 Инструкции",
    RecordType.FILES: "📁 Файлы и документы",
    RecordType.OTHER: "🗂 Прочее",
}

_CATEGORY_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_RECORD_TYPES = {record_type.value for record_type in RecordType}

# ---------------------------------------------------------------------------
# Динамическое меню по роли и правам (ТЗ §7.1, §7.3, §7.4)
# ---------------------------------------------------------------------------

# Права BOSS в seed: VIEW/CREATE/EDIT/DELETE/ARCHIVE/RESTORE/DOWNLOAD/UPLOAD/
# EXPORT/IMPORT/MANAGE_ACCESS/MANAGE_CATEGORIES/VIEW_AUDIT.


@dataclass(frozen=True)
class MenuTool:
    """Пункт меню: подпись, callback и требуемое системное право (если нужно)."""

    text: str
    callback: str
    permission: str | None = None


EMPLOYEE_TOOLS: tuple[MenuTool, ...] = (
    MenuTool("🔎 Поиск", "search:start"),
    MenuTool("⭐ Избранное", "favorites:list"),
    MenuTool("🕘 Недавнее", "recent:list"),
    MenuTool("👤 Мой профиль", "profile:view"),
)

BOSS_TOOLS: tuple[MenuTool, ...] = (
    MenuTool("➕ Добавить запись", "menu:add", PermissionCode.CREATE.value),
    MenuTool(
        "📂 Управление категориями", "category:manage", PermissionCode.MANAGE_CATEGORIES.value
    ),
    MenuTool("📎 Управление файлами", "menu:files", PermissionCode.UPLOAD.value),
    MenuTool("📥 Импорт", "import:csv", PermissionCode.IMPORT.value),
    MenuTool("📤 Экспорт", "export:csv", PermissionCode.EXPORT.value),
    MenuTool("🗃 Архив", "menu:archive", PermissionCode.ARCHIVE.value),
    MenuTool("⭐ Избранное", "favorites:list"),
    MenuTool("📊 Статистика", "menu:stats"),
    MenuTool("📝 История изменений", "menu:history"),
    MenuTool("🔎 Поиск", "search:start"),
)

SUPERADMIN_TOOLS: tuple[MenuTool, ...] = (
    MenuTool("👥 Пользователи", "admin:users", PermissionCode.MANAGE_USERS.value),
    MenuTool("🛡 Роли и доступы", "admin:roles", PermissionCode.MANAGE_SYSTEM.value),
    MenuTool("🏢 Группы сотрудников", "menu:groups", PermissionCode.MANAGE_GROUPS.value),
    MenuTool("📋 Полный аудит", "menu:audit", PermissionCode.VIEW_AUDIT.value),
    MenuTool("💾 Резервные копии", "menu:backup", PermissionCode.MANAGE_BACKUP.value),
    MenuTool("🔐 Безопасность", "menu:security", PermissionCode.MANAGE_SYSTEM.value),
    MenuTool("⚙️ Настройки системы", "menu:settings", PermissionCode.MANAGE_SYSTEM.value),
    MenuTool("🖥 Состояние сервисов", "menu:status", PermissionCode.MANAGE_SYSTEM.value),
    # Хаб администрирования виден всегда: ссылки внутри фильтруются по правам
    MenuTool("🧰 Администрирование", "menu:admin"),
)

MANAGER_ROLES = (RoleCode.BOSS, RoleCode.SUPERADMIN)


def _visible_tools(tools: tuple[MenuTool, ...], allowed: set[str]) -> list[MenuTool]:
    """Инструменты без системного права видны всегда, остальные — по правам роли."""
    return [tool for tool in tools if tool.permission is None or tool.permission in allowed]


def role_menu_sections(role: RoleCode | None, allowed: set[str] | None = None) -> list[list[MenuTool]]:
    """Секции главного меню для роли. Порядок секций соответствует ТЗ.

    EMPLOYEE — базовые инструменты, BOSS — инструменты раздела,
    SUPERADMIN — инструменты BOSS плюс административная секция.
    """
    allowed = set(allowed or ())
    if role == RoleCode.SUPERADMIN:
        sections = [_visible_tools(BOSS_TOOLS, allowed), _visible_tools(SUPERADMIN_TOOLS, allowed)]
    elif role == RoleCode.BOSS:
        sections = [_visible_tools(BOSS_TOOLS, allowed)]
    else:
        sections = [list(EMPLOYEE_TOOLS)]
    return [section for section in sections if section]


async def _allowed_system_permissions(user: User, session: AsyncSession) -> set[str]:
    """Собирает системные права пользователя для динамического меню."""
    candidates = {
        tool.permission
        for section in (BOSS_TOOLS, SUPERADMIN_TOOLS)
        for tool in section
        if tool.permission is not None
    }
    allowed: set[str] = set()
    for permission in sorted(candidates):
        if await PermissionService.can(user, permission, "system", None, session):
            allowed.add(permission)
    return allowed


def category_emoji(code: str) -> str:
    """Возвращает иконку раздела с запасным вариантом для пользовательских категорий."""
    return ROOT_EMOJI.get(code, "📁")


def category_title(code: str, name: str | None = None) -> str:
    if name:
        return f"{category_emoji(code)} {name.upper()}"
    return CATEGORY_NAMES.get(code, f"📁 {code}")


def breadcrumbs(*parts: str) -> str:
    return " › ".join(["🏠 Главная", *[part for part in parts if part]])


def can_manage_categories(user) -> bool:
    """Управление категориями доступно BOSS и SUPERADMIN."""
    return bool(user and user.role in (RoleCode.BOSS, RoleCode.SUPERADMIN))


def parse_list_callback(data: str | None) -> tuple[str, str, str, int] | None:
    """Разбирает menu:list:{category}:{type}:{sort}:{page}.

    Возвращает None для устаревших или некорректных callback_data.
    """
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 6 or parts[0] != "menu" or parts[1] != "list":
        return None
    _, _, category, record_type, sort, page_raw = parts
    if not _CATEGORY_CODE_RE.match(category):
        return None
    if record_type != "ALL" and record_type not in _RECORD_TYPES:
        return None
    if sort not in SORT_TITLES:
        return None
    try:
        page = int(page_raw)
    except ValueError:
        return None
    if page < 0:
        return None
    return category, record_type, sort, page


def category_content_keyboard(
    category_code: str, back_callback: str = "menu:main"
) -> InlineKeyboardMarkup:
    """Клавиатура типов информации внутри раздела."""
    buttons = [
        [
            InlineKeyboardButton(
                text=title,
                callback_data=f"menu:list:{category_code}:{record_type.value}:{SORT_DATE}:0",
            )
        ]
        for record_type, title in TYPE_TITLES.items()
    ]
    buttons.insert(
        0,
        [
            InlineKeyboardButton(
                text="📚 Все записи",
                callback_data=f"menu:list:{category_code}:ALL:{SORT_DATE}:0",
            )
        ],
    )
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _load_user(telegram_id: int):
    async with async_session() as session:
        return await get_user_by_telegram_id(session, telegram_id)


async def get_main_menu_keyboard(
    user=None, is_admin: bool = False, manage_categories: bool = False
) -> InlineKeyboardMarkup:
    """Клавиатура главного меню: категории по доступу + инструменты по роли и правам.

    Флаги is_admin/manage_categories оставлены для старых вызовов:
    достаточно роли и прав пользователя, флаг лишь принудительно повышает секцию.
    """
    buttons: list[list[InlineKeyboardButton]] = []
    role: RoleCode | None = getattr(user, "role", None)
    allowed: set[str] = set()
    if manage_categories:
        allowed.add(PermissionCode.MANAGE_CATEGORIES.value)
    if is_admin:
        role = RoleCode.SUPERADMIN

    if user is not None:
        async with async_session() as session:
            # Показываем только доступные категории
            categories = await list_categories(session)
            for cat in categories:
                if await PermissionService.can(user, "VIEW", "category", cat.id, session):
                    buttons.append(
                        [
                            InlineKeyboardButton(
                                text=f"{category_emoji(cat.code)} {cat.name}",
                                callback_data=f"menu:category:{cat.code}",
                            )
                        ]
                    )
            allowed |= await _allowed_system_permissions(user, session)

    for section in role_menu_sections(role, allowed):
        for tool in section:
            buttons.append([InlineKeyboardButton(text=tool.text, callback_data=tool.callback)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def build_main_menu(telegram_id: int) -> InlineKeyboardMarkup:
    """Клавиатура главного меню для /cancel и устаревших сценариев."""
    user = await _load_user(telegram_id)
    return await get_main_menu_keyboard(user=user)


async def render_main(callback: CallbackQuery) -> None:
    user = await _load_user(callback.from_user.id)
    await callback.message.edit_text(MAIN_TEXT, reply_markup=await get_main_menu_keyboard(user=user))


async def render_category(callback: CallbackQuery, category_code: str) -> bool:
    """Рисует раздел: типы информации, подкатегории и управление."""
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        category = await get_category(session, category_code)
        if category is None or category.status != "ACTIVE":
            return False
        if user is None or not await PermissionService.can(
            user, "VIEW", "category", category.id, session
        ):
            return False
        children = await list_categories(session, category.id)

    buttons = [
        [
            InlineKeyboardButton(
                text=title,
                callback_data=f"menu:list:{category.code}:{record_type.value}:{SORT_DATE}:0",
            )
        ]
        for record_type, title in TYPE_TITLES.items()
    ]
    buttons.insert(
        0,
        [
            InlineKeyboardButton(
                text="📚 Все записи",
                callback_data=f"menu:list:{category.code}:ALL:{SORT_DATE}:0",
            )
        ],
    )
    for child in children:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"📂 {child.name}",
                    callback_data=f"category:view:{child.code}",
                )
            ]
        )
    if can_manage_categories(user):
        buttons.append(
            [InlineKeyboardButton(text="📂 Управление категориями", callback_data="category:manage")]
        )
    # Поиск внутри выбранного раздела (ТЗ §7.2, §7.5)
    buttons.append(
        [
            InlineKeyboardButton(
                text="🔎 Поиск в разделе", callback_data=f"search:section:{category.code}"
            )
        ]
    )
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:back:menu:main")])

    title = category_title(category.code, category.name)
    await callback.message.edit_text(
        f"{breadcrumbs(title)}\n\nВыберите тип информации или подкатегорию:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    return True


async def render_records_list(
    callback: CallbackQuery,
    category_code: str,
    record_type: str,
    sort: str,
    page: int,
) -> bool:
    """Рисует страницу записей категории с сортировкой и пагинацией."""
    if record_type != "ALL" and record_type not in _RECORD_TYPES:
        return False
    if sort not in SORT_TITLES:
        return False
    page = max(page, 0)

    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not await is_user_active(user):
            return False
        category = await get_category(session, category_code)
        if category is None or category.status != "ACTIVE":
            return False
        if not await PermissionService.can(user, "VIEW", "category", category.id, session):
            return False

        rtype = None if record_type == "ALL" else RecordType(record_type)
        records, total = await list_records_page(
            session,
            category.code,
            record_type=rtype,
            sort="name" if sort == SORT_NAME else "date",
            offset=page * PAGE_SIZE,
            limit=PAGE_SIZE,
        )

    type_title = "📚 Все записи" if record_type == "ALL" else TYPE_TITLES[RecordType(record_type)]
    title = category_title(category.code, category.name)
    lines = [breadcrumbs(title, type_title), ""]
    if not records:
        lines.append("Записей пока нет.")
    else:
        for record in records:
            lines.append(f"• {record.title}")

    buttons = [
        [
            InlineKeyboardButton(
                text=f"📄 {record.title}", callback_data=f"record:view:{record.id}"
            )
        ]
        for record in records
    ]

    if can_manage_categories(user) and record_type != "ALL":
        buttons.append(
            [
                InlineKeyboardButton(
                    text="➕ Добавить запись",
                    callback_data=f"record:add:{record_type}:{category.code}",
                )
            ]
        )

    other_sort = SORT_NAME if sort == SORT_DATE else SORT_DATE
    sort_title = "🔤 По названию" if sort == SORT_DATE else "🕘 По дате"
    buttons.append(
        [
            InlineKeyboardButton(
                text=sort_title,
                callback_data=(
                    f"menu:list:{category.code}:{record_type}:{other_sort}:0"
                ),
            )
        ]
    )

    from app.bot.keyboards.pagination import pagination_keyboard

    pagination = pagination_keyboard(
        total, page, PAGE_SIZE, f"menu:list:{category.code}:{record_type}:{sort}"
    )
    buttons.extend(pagination.inline_keyboard)
    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"menu:back:menu:category:{category.code}",
            )
        ]
    )

    await callback.message.edit_text(
        "\n".join(lines) + f"\n\nСортировка: {SORT_TITLES[sort]}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    return True


async def open_screen(callback: CallbackQuery, target: str) -> bool:
    """Открывает экран по callback-адресу. False — адрес устарел."""
    if not target or target == "menu:main":
        await render_main(callback)
        return True

    if target.startswith("menu:category:"):
        return await render_category(callback, target.split(":", 2)[-1])

    if target.startswith("menu:list:"):
        parsed = parse_list_callback(target)
        if parsed is None:
            return False
        return await render_records_list(callback, *parsed)

    # Возврат на экран, который рисует другой роутер.
    if target.startswith("category:view:"):
        from app.bot.handlers.categories import category_view

        callback.data = target
        await category_view(callback)
        return True

    if target.startswith("record:view:"):
        from app.bot.handlers.records import view_record

        callback.data = target
        await view_record(callback)
        return True

    return False


@router.callback_query(F.data.startswith("menu:category:"))
async def menu_category(callback: CallbackQuery) -> None:
    category_code = (callback.data or "").split(":", 2)[-1]
    if await render_category(callback, category_code):
        await callback.answer()
        return
    await callback.answer(STALE_TEXT, show_alert=True)


@router.callback_query(F.data.startswith("menu:list:"))
async def menu_list(callback: CallbackQuery) -> None:
    parsed = parse_list_callback(callback.data)
    if parsed is None:
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    if await render_records_list(callback, *parsed):
        await callback.answer()
        return
    await callback.answer(STALE_TEXT, show_alert=True)


async def _ask_record_action(callback: CallbackQuery, action: str) -> None:
    record_id = (callback.data or "").split(":")[-1]
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not can_manage_categories(user):
            await callback.answer("Нет прав", show_alert=True)
            return
        try:
            record = await get_record(session, UUID(record_id))
        except ValueError:
            record = None
        if record is None:
            await callback.answer("Запись не найдена", show_alert=True)
            return
        title = record.title

    yes_text = "🗄 Архивировать" if action == "archive" else "🗑 Удалить"
    question = (
        "Переместить запись в архив?" if action == "archive" else "Удалить запись?"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=yes_text, callback_data=f"record:{action}:{record_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отмена", callback_data=f"record:view:{record_id}"
                ),
            ]
        ]
    )
    await callback.message.edit_text(
        f"<b>{question}</b>\n\nЗапись: {title}", reply_markup=kb, parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("menu:ask:archive:"))
async def menu_ask_archive(callback: CallbackQuery) -> None:
    await _ask_record_action(callback, "archive")


@router.callback_query(F.data.startswith("menu:ask:delete:"))
async def menu_ask_delete(callback: CallbackQuery) -> None:
    await _ask_record_action(callback, "delete")


# ---------------------------------------------------------------------------
# Экраны инструментов BOSS и SUPERADMIN
# ---------------------------------------------------------------------------


async def _guard(
    callback: CallbackQuery, permission: str | None = None, roles: tuple[RoleCode, ...] = ()
) -> bool:
    """Проверяет доступ к инструменту меню; при отказе сам отвечает на callback."""
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        if not await is_user_active(user):
            await callback.answer("Нет доступа", show_alert=True)
            return False
        if roles and user.role not in roles:
            await callback.answer("Недостаточно прав", show_alert=True)
            return False
        if permission is not None and not await PermissionService.can(
            user, permission, "system", None, session
        ):
            await callback.answer("Недостаточно прав", show_alert=True)
            return False
    return True


async def _screen(
    callback: CallbackQuery,
    title: str,
    lines: list[str],
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> None:
    """Однотипный экран инструмента с возвратом в главное меню."""
    rows = list(extra_rows or [])
    rows.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu:main")])
    body = "\n".join(lines) if lines else "—"
    await callback.message.edit_text(
        f"{breadcrumbs(title)}\n\n{body}", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await callback.answer()


@router.callback_query(F.data == "menu:add")
async def menu_add(callback: CallbackQuery) -> None:
    """Выбор раздела для нового записания: разделы с правом CREATE."""
    if not await _guard(callback, PermissionCode.CREATE.value):
        return
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        categories = await list_categories(session)
        rows = []
        for cat in categories:
            if await PermissionService.can(user, "CREATE", "category", cat.id, session):
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"{category_emoji(cat.code)} {cat.name}",
                            callback_data=f"menu:add_cat:{cat.code}",
                        )
                    ]
                )
    if not rows:
        await _screen(callback, "➕ Добавить запись", ["Нет разделов, доступных для создания записей."])
        return
    await _screen(callback, "➕ Добавить запись", ["Выберите раздел:"], extra_rows=rows)


@router.callback_query(F.data.startswith("menu:add_cat:"))
async def menu_add_category(callback: CallbackQuery) -> None:
    """Выбор типа информации для новой записи."""
    code = (callback.data or "").removeprefix("menu:add_cat:")
    if not await _guard(callback, PermissionCode.CREATE.value):
        return
    async with async_session() as session:
        category = await get_category(session, code)
    if category is None or category.status != "ACTIVE":
        await callback.answer(STALE_TEXT, show_alert=True)
        return
    rows = [
        [
            InlineKeyboardButton(
                text=title, callback_data=f"record:add:{record_type.value}:{category.code}"
            )
        ]
        for record_type, title in TYPE_TITLES.items()
    ]
    title = category_title(category.code, category.name)
    await _screen(callback, f"➕ {title}", ["Выберите тип информации:"], extra_rows=rows)


@router.callback_query(F.data == "menu:files")
async def menu_files(callback: CallbackQuery) -> None:
    """Хаб управления файлами: загрузка и импорт/экспорт CSV."""
    if not await _guard(callback, PermissionCode.UPLOAD.value):
        return
    rows = [
        [InlineKeyboardButton(text="📤 Загрузить файл", callback_data="files:upload")],
        [InlineKeyboardButton(text="📥 Импорт CSV", callback_data="import:csv")],
        [InlineKeyboardButton(text="📤 Экспорт CSV", callback_data="export:csv")],
    ]
    await _screen(callback, "📎 Управление файлами", ["Выберите действие:"], extra_rows=rows)


@router.callback_query(F.data == "menu:archive")
async def menu_archive(callback: CallbackQuery) -> None:
    """Последние записи в архиве. Восстановление — из карточки записи (Phase 05)."""
    if not await _guard(callback, PermissionCode.ARCHIVE.value):
        return
    async with async_session() as session:
        total = (
            await session.scalar(
                select(func.count()).select_from(Record).where(Record.status == "ARCHIVED")
            )
            or 0
        )
        records = (
            (
                await session.execute(
                    select(Record)
                    .where(Record.status == "ARCHIVED")
                    .order_by(Record.updated_at.desc())
                    .limit(PAGE_SIZE)
                )
            )
            .scalars()
            .all()
        )
    rows = [
        [InlineKeyboardButton(text=f"📄 {record.title}", callback_data=f"record:view:{record.id}")]
        for record in records
    ]
    lines = [f"В архиве: {total} записей.", ""]
    lines.append("Ниже — последние из них." if rows else "Архив пуст.")
    await _screen(callback, "🗃 Архив", lines, extra_rows=rows)


@router.callback_query(F.data == "menu:stats")
async def menu_stats(callback: CallbackQuery) -> None:
    """Сводная статистика по записям, файлам и пользователям."""
    if not await _guard(callback, roles=MANAGER_ROLES):
        return
    async with async_session() as session:

        async def _count(*conditions) -> int:
            return (
                await session.scalar(select(func.count()).select_from(Record).where(*conditions))
                or 0
            )

        active = await _count(Record.status == "ACTIVE")
        archived = await _count(Record.status == "ARCHIVED")
        deleted = await _count(Record.status == "DELETED")
        files = (
            await session.scalar(
                select(func.count()).select_from(File).where(File.deleted_at.is_(None))
            )
            or 0
        )
        users = (
            await session.scalar(
                select(func.count()).select_from(User).where(User.status == UserStatus.ACTIVE)
            )
            or 0
        )
        categories = (
            await session.scalar(
                select(func.count())
                .select_from(Category)
                .where(Category.status == "ACTIVE", Category.parent_id.is_(None))
            )
            or 0
        )
        by_type = (
            (
                await session.execute(
                    select(Record.record_type, func.count())
                    .where(Record.status == "ACTIVE")
                    .group_by(Record.record_type)
                )
            )
            .all()
        )

    lines = [
        f"📚 Активных записей: <b>{active}</b>",
        f"🗃 В архиве: {archived}",
        f"🗑 Удалено: {deleted}",
        f"📁 Файлов: {files}",
        f"👥 Активных пользователей: {users}",
        f"🏢 Разделов: {categories}",
        "",
        "Активные записи по типам:",
    ]
    if by_type:
        for record_type, count in sorted(by_type, key=lambda row: -row[1]):
            key = getattr(record_type, "value", record_type)
            lines.append(f"• {TYPE_TITLES.get(RecordType(key), str(key))}: {count}")
    else:
        lines.append("—")
    await _screen(callback, "📊 Статистика", lines)


@router.callback_query(F.data == "menu:history")
async def menu_history(callback: CallbackQuery) -> None:
    """Последние изменения записей (история версий)."""
    if not await _guard(callback, roles=MANAGER_ROLES):
        return
    async with async_session() as session:
        rows_data = (
            (
                await session.execute(
                    select(RecordVersion, Record.title)
                    .join(Record, Record.id == RecordVersion.record_id)
                    .order_by(RecordVersion.created_at.desc())
                    .limit(15)
                )
            )
            .all()
        )
    if not rows_data:
        await _screen(callback, "📝 История изменений", ["Изменений пока нет."])
        return
    lines = []
    for version, record_title in rows_data:
        when = version.created_at.strftime("%d.%m.%Y %H:%M") if version.created_at else "—"
        note = html.escape((version.change_description or "").strip() or "изменение")
        lines.append(f"• {when} — v{version.version} — <b>{html.escape(record_title)}</b> — {note}")
    await _screen(callback, "📝 История изменений", lines)


@router.callback_query(F.data == "menu:groups")
async def menu_groups(callback: CallbackQuery) -> None:
    """Список групп сотрудников и состав (управление — admin:users → группы)."""
    if not await _guard(callback, PermissionCode.MANAGE_GROUPS.value):
        return
    async with async_session() as session:
        rows_data = (
            (
                await session.execute(
                    select(Group, func.count(GroupMember.user_id))
                    .outerjoin(GroupMember, GroupMember.group_id == Group.id)
                    .group_by(Group.id)
                    .order_by(Group.name)
                )
            )
            .all()
        )
    lines = [
        f"• {html.escape(group.name)} (<code>{html.escape(group.code)}</code>) — участников: {count}"
        for group, count in rows_data
    ]
    await _screen(callback, "🏢 Группы сотрудников", lines or ["Групп пока нет."])


@router.callback_query(F.data == "menu:audit")
async def menu_audit(callback: CallbackQuery) -> None:
    """Последние записи журнала аудита."""
    if not await _guard(callback, PermissionCode.VIEW_AUDIT.value):
        return
    async with async_session() as session:
        logs = (
            (
                await session.execute(
                    select(AuditLog).order_by(AuditLog.created_at.desc()).limit(20)
                )
            )
            .scalars()
            .all()
        )
    lines = []
    for log in logs:
        when = log.created_at.strftime("%d.%m.%Y %H:%M") if log.created_at else "—"
        lines.append(
            f"• {when} — <code>{html.escape(log.action)}</code> — {html.escape(log.object_type)} — "
            f"{html.escape(log.result)}"
        )
    if not lines:
        lines = ["Журнал аудита пуст."]
    lines += ["", "Фильтры и полная история — в веб-интерфейсе (после Phase 04)."]
    await _screen(callback, "📋 Полный аудит", lines)


@router.callback_query(F.data == "menu:backup")
async def menu_backup(callback: CallbackQuery) -> None:
    """Список последних резервных копий. Запуск копирования — Phase 10."""
    if not await _guard(callback, PermissionCode.MANAGE_BACKUP.value):
        return
    async with async_session() as session:
        backups = (
            (
                await session.execute(
                    select(Backup).order_by(Backup.created_at.desc()).limit(10)
                )
            )
            .scalars()
            .all()
        )
    lines = []
    for backup in backups:
        when = backup.created_at.strftime("%d.%m.%Y %H:%M") if backup.created_at else "—"
        size = f"{backup.size / 1024 / 1024:.1f} МБ" if backup.size else "—"
        lines.append(f"• {when} — {backup.kind.value} — {backup.status.value} — {size}")
    if not lines:
        lines = ["Резервных копий ещё нет."]
    lines += ["", "Запуск и восстановление бэкапов будут доступны в Phase 10."]
    await _screen(callback, "💾 Резервные копии", lines)


@router.callback_query(F.data == "menu:security")
async def menu_security(callback: CallbackQuery) -> None:
    """Последние события безопасности и количество заблокированных."""
    if not await _guard(callback, PermissionCode.MANAGE_SYSTEM.value):
        return
    async with async_session() as session:
        events = (
            (
                await session.execute(
                    select(SecurityEvent).order_by(SecurityEvent.created_at.desc()).limit(20)
                )
            )
            .scalars()
            .all()
        )
        blocked = (
            await session.scalar(
                select(func.count()).select_from(User).where(User.status == UserStatus.BLOCKED)
            )
            or 0
        )
    lines = [f"🚫 Заблокированных пользователей: {blocked}", ""]
    for event in events:
        when = event.created_at.strftime("%d.%m.%Y %H:%M") if event.created_at else "—"
        lines.append(f"• {when} — [{html.escape(event.severity)}] {html.escape(event.event_type)}")
    if len(lines) <= 2:
        lines.append("Событий безопасности нет.")
    await _screen(callback, "🔐 Безопасность", lines)


@router.callback_query(F.data == "menu:settings")
async def menu_settings(callback: CallbackQuery) -> None:
    """Список системных настроек (значения скрыты)."""
    if not await _guard(callback, PermissionCode.MANAGE_SYSTEM.value):
        return
    async with async_session() as session:
        settings_rows = (
            (
                await session.execute(
                    select(SystemSetting).order_by(SystemSetting.key).limit(30)
                )
            )
            .scalars()
            .all()
        )
    lines = [
        f"• <code>{html.escape(item.key)}</code> — "
        f"{'значение установлено' if item.value is not None else 'не задано'}"
        for item in settings_rows
    ]
    if not lines:
        lines = ["Системные настройки ещё не создавались."]
    lines += ["", "Редактирование настроек — в поздних фазах."]
    await _screen(callback, "⚙️ Настройки системы", lines)


@router.callback_query(F.data == "menu:status")
async def menu_status(callback: CallbackQuery) -> None:
    """Состояние сервисов: проверка доступности базы данных."""
    if not await _guard(callback, PermissionCode.MANAGE_SYSTEM.value):
        return
    db_line = "🗄 База данных: <b>ОШИБКА</b>"
    try:
        async with async_session() as session:
            started = time.perf_counter()
            await session.execute(text("SELECT 1"))
            elapsed_ms = (time.perf_counter() - started) * 1000
        db_line = f"🗄 База данных: <b>OK</b> ({elapsed_ms:.0f} мс)"
    except Exception:  # noqa: BLE001 — показываем деградацию, не роняем меню
        db_line = "🗄 База данных: <b>ОШИБКА</b>"
    lines = [
        db_line,
        "🧠 Redis и 🪣 хранилище файлов проверяются фоновыми воркерами (Phase 11).",
    ]
    await _screen(callback, "🖥 Состояние сервисов", lines)


@router.callback_query(F.data == "menu:admin")
async def menu_admin(callback: CallbackQuery) -> None:
    """Хаб администрирования: только инструменты, разрешённые текущему пользователю."""
    if not await _guard(callback, roles=(RoleCode.SUPERADMIN,)):
        return
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, callback.from_user.id)
        allowed = await _allowed_system_permissions(user, session)
    rows = [
        [InlineKeyboardButton(text=tool.text, callback_data=tool.callback)]
        for tool in _visible_tools(SUPERADMIN_TOOLS, allowed)
    ]
    await _screen(callback, "🧰 Администрирование", ["Выберите инструмент:", ""], extra_rows=rows)


__all__ = [
    "BOSS_TOOLS",
    "CATEGORY_NAMES",
    "EMPLOYEE_TOOLS",
    "MAIN_TEXT",
    "MANAGER_ROLES",
    "PAGE_SIZE",
    "SORT_DATE",
    "SORT_NAME",
    "SORT_TITLES",
    "STALE_TEXT",
    "SUPERADMIN_TOOLS",
    "TYPE_TITLES",
    "MenuTool",
    "breadcrumbs",
    "build_main_menu",
    "category_emoji",
    "category_title",
    "get_main_menu_keyboard",
    "open_screen",
    "parse_list_callback",
    "render_main",
    "render_records_list",
    "role_menu_sections",
    "router",
]
