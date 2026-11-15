import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.handlers import categories
from app.bot.handlers.menu import (
    SORT_DATE,
    SORT_NAME,
    category_content_keyboard,
    category_emoji,
    parse_list_callback,
)
from app.database.models import Category, Record, RecordType, RoleCode, User
from app.services.categories import (
    CategoryConflict,
    CategoryError,
    CategoryInUseError,
    CategoryNotFound,
    CategoryValidationError,
    archive_category,
    count_category_records,
    create_category,
    delete_category,
    get_category,
    get_category_by_id,
    list_categories,
    update_category,
    validate_category_code,
    validate_category_name,
)

TELEGRAM_CALLBACK_LIMIT = 64


# ---------------------------------------------------------------------------
# Чистые тесты: валидация и клавиатуры (не требуют БД)
# ---------------------------------------------------------------------------


def test_validate_category_code_normalizes_input():
    assert validate_category_code("  Holodka_2 ") == "holodka_2"
    assert validate_category_code("a-b") == "a-b"


@pytest.mark.parametrize(
    "code",
    ["", " ", "a", "Upper Space", "кириллица", "bad:code", "x" * 33],
)
def test_validate_category_code_rejects_invalid_values(code):
    with pytest.raises(CategoryValidationError):
        validate_category_code(code)


def test_validate_category_name_trims_and_limits():
    assert validate_category_name("  Продажи  ") == "Продажи"
    with pytest.raises(CategoryValidationError):
        validate_category_name("   ")
    with pytest.raises(CategoryValidationError):
        validate_category_name("x" * 129)


def test_category_errors_are_value_errors():
    for error in (CategoryConflict, CategoryInUseError, CategoryNotFound):
        assert issubclass(error, CategoryError)
        assert issubclass(error, ValueError)


def test_category_emoji_falls_back_for_custom_code():
    assert category_emoji("holodka") == "❄️"
    assert category_emoji("custom_section") == "📁"


def test_category_content_keyboard_has_all_record_types():
    kb = category_content_keyboard("custom")
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "menu:list:custom:ALL:d:0" in callbacks
    for record_type in RecordType:
        assert f"menu:list:custom:{record_type.value}:{SORT_DATE}:0" in callbacks
        assert len(f"menu:list:custom:{record_type.value}:{SORT_DATE}:0") <= (
            TELEGRAM_CALLBACK_LIMIT
        )


def test_dynamic_category_callbacks_fit_telegram_limit():
    # Максимальная длина кода ограничена сервисом категорий.
    code = "a" * 32
    for record_type in RecordType:
        callback = f"menu:list:{code}:{record_type.value}:{SORT_DATE}:99"
        assert len(callback.encode()) <= TELEGRAM_CALLBACK_LIMIT


def test_categories_router_exposes_management_handlers():
    handler_names = {handler.callback.__name__ for handler in categories.router.callback_query.handlers}
    assert {
        "category_manage",
        "category_view",
        "category_content",
        "category_add_root_choice",
        "category_add_start",
        "category_edit_start",
        "category_archive",
        "category_delete",
        "category_delete_confirm",
    } <= handler_names


@pytest.mark.parametrize(
    "data,expected",
    [
        ("menu:list:second_stage:INSTRUCTIONS:n:3", ("second_stage", "INSTRUCTIONS", "n", 3)),
        ("menu:list:holodka:ALL:d:0", ("holodka", "ALL", "d", 0)),
        ("menu:list:custom_sub:OTHER:d:12", ("custom_sub", "OTHER", "d", 12)),
    ],
)
def test_parse_list_callback_accepts_valid_data(data, expected):
    assert parse_list_callback(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        None,
        "",
        "menu:list:holodka:UNKNOWN:d:0",
        "menu:list:holodka:LINKS:x:0",
        "menu:list:holodka:LINKS:d:-1",
        "menu:list:holodka:LINKS:d:abc",
        "menu:list:holodka:LINKS:d",
        "menu:list::LINKS:d:0",
        "menu:category:holodka",
    ],
)
def test_parse_list_callback_rejects_stale_data(data):
    assert parse_list_callback(data) is None


# ---------------------------------------------------------------------------
# Интеграционные тесты сервиса (нужен TEST_DATABASE_URL)
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_session():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated PostgreSQL test database")

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as session:
            try:
                await session.execute(select(Category.id).limit(1))
            except SQLAlchemyError:
                pytest.skip("Apply migrations to the test database first")
            yield session
    finally:
        await engine.dispose()


async def _drop_categories(session: AsyncSession, category_ids: list) -> None:
    if not category_ids:
        return
    await session.execute(delete(Category).where(Category.id.in_(category_ids)))
    await session.commit()


def _unique_code(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


async def test_create_get_and_list_categories(db_session):
    root_code = _unique_code("root")
    child_code = _unique_code("child")
    created = []
    try:
        root = await create_category(
            db_session, root_code, "Тестовый раздел", category_type="root", sort_order=90
        )
        created.append(root.id)
        child = await create_category(
            db_session, child_code, "Подкатегория", parent_id=root.id, sort_order=1
        )
        created.append(child.id)

        assert (await get_category(db_session, root_code)).id == root.id
        assert (await get_category_by_id(db_session, child.id)).code == child_code
        assert await get_category(db_session, "missing_code") is None

        roots = await list_categories(db_session)
        assert root.id in {category.id for category in roots}

        children = await list_categories(db_session, root.id)
        assert [category.id for category in children] == [child.id]

        with pytest.raises(CategoryConflict):
            await create_category(db_session, root_code, "Дубликат")
        with pytest.raises(CategoryNotFound):
            await create_category(
                db_session, _unique_code("orphan"), "Сирота", parent_id=uuid4()
            )
        with pytest.raises(CategoryValidationError):
            await create_category(db_session, "bad code", "Плохой код")
    finally:
        await _drop_categories(db_session, list(reversed(created)))


async def test_update_and_archive_category(db_session):
    root_code = _unique_code("root")
    child_code = _unique_code("child")
    created = []
    try:
        root = await create_category(db_session, root_code, "Раздел", category_type="root")
        created.append(root.id)
        child = await create_category(db_session, child_code, "Старое имя", parent_id=root.id)
        created.append(child.id)

        updated = await update_category(
            db_session, child.id, name="Новое имя", sort_order=7, status="DISABLED"
        )
        assert updated.name == "Новое имя"
        assert updated.sort_order == 7
        assert updated.status == "DISABLED"

        archived = await archive_category(db_session, child.id)
        assert archived.status == "ARCHIVED"
        assert await list_categories(db_session, root.id) == []
        archived_children = await list_categories(
            db_session, root.id, include_archived=True
        )
        assert [category.id for category in archived_children] == [child.id]

        with pytest.raises(CategoryValidationError):
            await update_category(db_session, child.id, status="UNKNOWN")
        with pytest.raises(CategoryNotFound):
            await update_category(db_session, uuid4(), name="Нет такой")

        system = await get_category(db_session, "general")
        if system is not None:
            with pytest.raises(CategoryValidationError, match="переименовать"):
                await update_category(db_session, system.id, name="Другое имя")
            with pytest.raises(CategoryValidationError, match="архивировать"):
                await archive_category(db_session, system.id)
    finally:
        await _drop_categories(db_session, list(reversed(created)))


async def test_delete_category_blocks_children_and_system_roots(db_session):
    root_code = _unique_code("root")
    middle_code = _unique_code("middle")
    leaf_code = _unique_code("leaf")
    created = []
    try:
        root = await create_category(db_session, root_code, "Раздел", category_type="root")
        created.append(root.id)
        middle = await create_category(db_session, middle_code, "Середина", parent_id=root.id)
        created.append(middle.id)
        leaf = await create_category(db_session, leaf_code, "Лист", parent_id=middle.id)
        created.append(leaf.id)

        with pytest.raises(CategoryInUseError, match="вложенные"):
            await delete_category(db_session, middle.id)

        system = await get_category(db_session, "general")
        if system is not None:
            with pytest.raises(CategoryInUseError, match="Системный раздел"):
                await delete_category(db_session, system.id)

        await delete_category(db_session, leaf.id)
        created.remove(leaf.id)
        assert await get_category_by_id(db_session, leaf.id) is None

        await delete_category(db_session, middle.id)
        created.remove(middle.id)
        await delete_category(db_session, root.id)
        created.remove(root.id)
    finally:
        await _drop_categories(db_session, list(reversed(created)))


async def test_delete_category_blocks_linked_records(db_session):
    user = (await db_session.execute(select(User).limit(1))).scalar_one_or_none()
    if user is None:
        pytest.skip("The test database has no users")

    root_code = _unique_code("root")
    child_code = _unique_code("child")
    created = []
    record = None
    try:
        root = await create_category(db_session, root_code, "Раздел", category_type="root")
        created.append(root.id)
        child = await create_category(db_session, child_code, "С записями", parent_id=root.id)
        created.append(child.id)

        record = Record(
            category_id=root.id,
            subcategory_id=child.id,
            record_type=RecordType.OTHER,
            title="Тестовая запись",
            created_by=user.id,
        )
        db_session.add(record)
        await db_session.commit()

        assert await count_category_records(db_session, child.id) == 1
        with pytest.raises(CategoryInUseError, match="записи"):
            await delete_category(db_session, child.id)
    finally:
        if record is not None:
            await db_session.delete(record)
            await db_session.commit()
        await _drop_categories(db_session, list(reversed(created)))


def test_management_roles_can_be_checked_without_database():
    class StubUser:
        def __init__(self, role):
            self.role = role

    assert categories.can_manage_categories(StubUser(RoleCode.BOSS)) is True
    assert categories.can_manage_categories(StubUser(RoleCode.SUPERADMIN)) is True
    assert categories.can_manage_categories(StubUser(RoleCode.EMPLOYEE)) is False
    assert categories.can_manage_categories(None) is False


def test_sort_constants_are_supported_by_parser():
    assert parse_list_callback(f"menu:list:general:ALL:{SORT_DATE}:0") is not None
    assert parse_list_callback(f"menu:list:general:ALL:{SORT_NAME}:0") is not None
