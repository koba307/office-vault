"""Тесты динамических меню Phase 04: роли, права, инструменты и поиск в разделе.

Тесты не требуют базы: проверяется чистая логика построения секций,
разбор callback_data и тексты start.py.
"""

from app.bot.handlers import menu, search
from app.bot.handlers.menu import (
    BOSS_TOOLS,
    EMPLOYEE_TOOLS,
    SUPERADMIN_TOOLS,
    MenuTool,
    role_menu_sections,
)
from app.bot.handlers.start import invitation_text, role_title
from app.database.models import PermissionCode, RecordType, RoleCode

TELEGRAM_CALLBACK_LIMIT = 64

ALL_SYSTEM_PERMISSIONS = {code.value for code in PermissionCode}

# Тексты меню строго по ТЗ §7.3
TZ_BOSS_LABELS = [
    "➕ Добавить запись",
    "📂 Управление категориями",
    "📎 Управление файлами",
    "📥 Импорт",
    "📤 Экспорт",
    "🗃 Архив",
    "⭐ Избранное",
    "📊 Статистика",
    "📝 История изменений",
    "🔎 Поиск",
]

# Тексты меню строго по ТЗ §7.4
TZ_SUPERADMIN_LABELS = [
    "👥 Пользователи",
    "🛡 Роли и доступы",
    "🏢 Группы сотрудников",
    "📋 Полный аудит",
    "💾 Резервные копии",
    "🔐 Безопасность",
    "📥 Заявки на регистрацию",
]


def _labels(sections: list[list[MenuTool]]) -> list[str]:
    return [tool.text for section in sections for tool in section]


# ---------------------------------------------------------------------------
# Динамическое построение меню по роли и правам
# ---------------------------------------------------------------------------


def test_employee_menu_has_only_categories():
    sections = role_menu_sections(RoleCode.EMPLOYEE, ALL_SYSTEM_PERMISSIONS)
    assert sections == []


def test_employee_role_none_falls_back_to_base_tools():
    # Незарегистрированный или повреждённый контекст не должен получать инструменты BOSS
    assert role_menu_sections(None) == []


def test_boss_menu_with_full_permissions_matches_tz():
    sections = role_menu_sections(RoleCode.BOSS, ALL_SYSTEM_PERMISSIONS)
    assert len(sections) == 1
    assert [tool.text for tool in sections[0]] == TZ_BOSS_LABELS


def test_boss_menu_hides_tools_without_permissions():
    allowed = {
        PermissionCode.VIEW.value,
        PermissionCode.CREATE.value,
        PermissionCode.ARCHIVE.value,
    }
    labels = _labels(role_menu_sections(RoleCode.BOSS, allowed))
    assert "➕ Добавить запись" in labels
    assert "🗃 Архив" in labels
    # Права IMPORT/EXPORT/UPLOAD/MANAGE_CATEGORIES у этой роли отсутствуют
    assert "📥 Импорт" not in labels
    assert "📤 Экспорт" not in labels
    assert "📎 Управление файлами" not in labels
    assert "📂 Управление категориями" not in labels


def test_boss_without_permissions_keeps_permissionless_tools():
    labels = _labels(role_menu_sections(RoleCode.BOSS, set()))
    assert labels == ["⭐ Избранное", "📊 Статистика", "📝 История изменений", "🔎 Поиск"]


def test_boss_menu_never_contains_superadmin_tools():
    labels = _labels(role_menu_sections(RoleCode.BOSS, ALL_SYSTEM_PERMISSIONS))
    for admin_label in TZ_SUPERADMIN_LABELS:
        if admin_label not in {tool.text for tool in BOSS_TOOLS}:
            assert admin_label not in labels


def test_superadmin_menu_is_boss_plus_admin_sections():
    sections = role_menu_sections(RoleCode.SUPERADMIN, ALL_SYSTEM_PERMISSIONS)
    assert len(sections) == 2
    assert [tool.text for tool in sections[0]] == TZ_BOSS_LABELS
    assert [tool.text for tool in sections[1]] == TZ_SUPERADMIN_LABELS


def test_superadmin_sections_filter_independently():
    allowed = {
        PermissionCode.MANAGE_USERS.value,
        PermissionCode.VIEW_AUDIT.value,
        PermissionCode.CREATE.value,
        PermissionCode.UPLOAD.value,
        PermissionCode.IMPORT.value,
        PermissionCode.EXPORT.value,
        PermissionCode.ARCHIVE.value,
        PermissionCode.MANAGE_CATEGORIES.value,
    }
    sections = role_menu_sections(RoleCode.SUPERADMIN, allowed)
    boss_labels = [tool.text for tool in sections[0]]
    admin_labels = [tool.text for tool in sections[1]]
    assert "➕ Добавить запись" in boss_labels
    assert "👥 Пользователи" in admin_labels
    assert "📋 Полный аудит" in admin_labels
    assert "🛡 Роли и доступы" not in admin_labels
    assert "💾 Резервные копии" not in admin_labels
    assert "⚙️ Настройки системы" not in admin_labels
    assert "🧰 Администрирование" not in admin_labels


def test_sections_are_never_empty_lists():
    for role in (RoleCode.EMPLOYEE, RoleCode.BOSS, RoleCode.COACH, RoleCode.SUPERADMIN, None):
        for allowed in (set(), ALL_SYSTEM_PERMISSIONS):
            assert all(section for section in role_menu_sections(role, allowed))


def test_all_tool_callbacks_fit_telegram_limit():
    for tools in (EMPLOYEE_TOOLS, BOSS_TOOLS, SUPERADMIN_TOOLS):
        for tool in tools:
            assert len(tool.callback.encode()) <= TELEGRAM_CALLBACK_LIMIT, tool.callback
            assert tool.text and tool.callback


# ---------------------------------------------------------------------------
# Экраны инструментов: callbacks существуют и адресуют известные роутеры
# ---------------------------------------------------------------------------


def test_boss_tools_point_to_routable_callbacks():
    # Экраны menu:* рисует menu.py, остальное — существующие роутеры
    for tool in BOSS_TOOLS:
        prefix = tool.callback.split(":")[0]
        assert prefix in {"menu", "category", "import", "export", "favorites", "search"}


def test_search_in_section_callback_shape():
    data = "search:section:second_stage"
    assert len(data.encode()) <= TELEGRAM_CALLBACK_LIMIT
    assert search.parse_section_callback(data) == "second_stage"


# ---------------------------------------------------------------------------
# Поиск в разделе: разбор callback_data и навигация
# ---------------------------------------------------------------------------


def test_parse_section_callback_variants():
    assert search.parse_section_callback(None) is None
    assert search.parse_section_callback("") is None
    assert search.parse_section_callback("search:start") is None
    assert search.parse_section_callback("search:section:") is None
    assert search.parse_section_callback("search:section:holodka") == "holodka"


def test_parse_filter_callback_normalizes_and_rejects_stale():
    assert search.parse_filter_callback("search:filter:credentials") == "CREDENTIALS"
    assert search.parse_filter_callback("search:filter:INSTRUCTIONS") == "INSTRUCTIONS"
    assert search.parse_filter_callback("search:filter:all") == "ALL"
    assert search.parse_filter_callback("search:filter:GHOST") is None
    assert search.parse_filter_callback("search:filter:") is None
    assert search.parse_filter_callback("record:view:1") is None


def test_parse_page_callback():
    assert search.parse_page_callback("search:page:0") == 0
    assert search.parse_page_callback("search:page:12") == 12
    assert search.parse_page_callback("search:page:-1") is None
    assert search.parse_page_callback("search:page:abc") is None
    assert search.parse_page_callback("search:page:") is None
    assert search.parse_page_callback("menu:list:x:ALL:d:0") is None


def test_every_record_type_is_search_filter():
    for record_type in RecordType:
        assert record_type.value in search.FILTERS


def test_filter_rows_marks_active_and_splits_rows():
    rows = search.filter_rows("SIP")
    callbacks = [btn.callback_data for row in rows for btn in row]
    texts = [btn.text for row in rows for btn in row]
    assert len(callbacks) == len(search.FILTERS)
    assert all(len(cb.encode()) <= TELEGRAM_CALLBACK_LIMIT for cb in callbacks)
    assert "• ☎️" in texts
    assert "search:filter:sip" in callbacks
    # Ряды не шире четырёх кнопок
    assert all(len(row) <= 4 for row in rows)
    assert len(rows) == 2


def test_results_pagination_window_math():
    total = 12
    pages = search.total_pages(total, search.PAGE_SIZE)
    assert pages == 3
    # Окно второй страницы при PAGE_SIZE=5
    window = list(range(total))[1 * search.PAGE_SIZE : 1 * search.PAGE_SIZE + search.PAGE_SIZE]
    assert window == [5, 6, 7, 8, 9]


def test_results_markup_contains_filters_and_navigation():
    markup = search._results_markup([], 0, 0, "ALL")
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "search:back" in callbacks
    assert "menu:main" in callbacks
    assert "search:filter:credentials" in callbacks


# ---------------------------------------------------------------------------
# start.py: роль и приглашение
# ---------------------------------------------------------------------------


def test_role_titles_human_readable():
    assert role_title(RoleCode.SUPERADMIN) == "Суперадминистратор"
    assert role_title("BOSS") == "Руководитель"
    assert role_title(RoleCode.EMPLOYEE) == "Сотрудник"
    assert role_title(None) == "Сотрудник"


def test_invitation_text_contains_id_and_no_menu():
    text = invitation_text(12345, "Иван")
    assert "12345" in text
    assert "зарегистрированным" in text
    # Незарегистрированным меню не показывается — в тексте нет кнопок-разделов
    assert "Выберите рабочий раздел" not in text


# ---------------------------------------------------------------------------
# Меню раздела: типы информации + поиск в разделе (ТЗ §7.2)
# ---------------------------------------------------------------------------


def test_category_keyboard_has_all_types_and_back():
    kb = menu.category_content_keyboard("holodka")
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "menu:list:holodka:ALL:d:0" in callbacks
    for record_type in RecordType:
        assert any(record_type.value in cb for cb in callbacks)
    assert callbacks[-1] == "menu:main"
