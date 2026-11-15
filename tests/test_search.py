"""Тесты permission-aware поиска Phase 07 (ТЗ §11).

Чистые тесты проверяют нормализацию запроса, фильтры и callback-логику.
Интеграционные тесты требуют TEST_DATABASE_URL и проверяют реальную выдачу:
закрытые, архивные и удалённые записи не должны попадать в результаты.
"""

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.handlers import menu
from app.bot.handlers import search as search_handlers
from app.database.models import (
    AccessMode,
    Category,
    CategoryPermission,
    FieldType,
    File,
    Permission,
    PermissionCode,
    Record,
    RecordField,
    RecordFile,
    RecordPermission,
    RecordTag,
    RecordType,
    Role,
    RoleCode,
    RolePermission,
    Tag,
    User,
    UserStatus,
)
from app.services.search import service as search_service
from app.services.search.service import (
    normalize_query,
    normalize_record_type,
    normalize_tags,
    record_types_for_query,
    record_types_matching,
    search_records,
    tokenize_query,
)

TELEGRAM_CALLBACK_LIMIT = 64


# ---------------------------------------------------------------------------
# Чистые тесты: нормализация запроса и фильтров
# ---------------------------------------------------------------------------


def test_normalize_query_is_case_insensitive_and_collapses_spaces():
    assert normalize_query("  CRM   Alpha  ") == "crm alpha"
    assert normalize_query(None) == ""
    assert len(normalize_query("x" * 500)) == search_service.MAX_QUERY_LENGTH


def test_tokenize_query_removes_duplicates_and_preserves_order():
    assert tokenize_query("CRM crm  Alpha") == ["crm", "alpha"]
    assert tokenize_query("   ") == []
    assert tokenize_query(None) == []


def test_record_types_are_searchable_by_code_and_russian_title():
    assert RecordType.SIP in record_types_matching("sip")
    assert RecordType.SIP in record_types_matching("телефония")
    assert RecordType.CREDENTIALS in record_types_matching("пароли")
    assert RecordType.FILES in record_types_matching("документы")
    assert record_types_matching("") == ()


def test_record_types_for_query_requires_all_words():
    assert record_types_for_query("логины и пароли") == (RecordType.CREDENTIALS,)
    assert record_types_for_query("sip телефония") == (RecordType.SIP,)
    # Лишнее слово уже не описывает тип целиком
    assert record_types_for_query("sip server") == ()


def test_normalize_tags_accepts_string_sequence_and_empty_values():
    assert normalize_tags("  Важно  ") == ["важно"]
    assert normalize_tags(["VIP", "vip", "  "]) == ["vip"]
    assert normalize_tags(None) == []
    assert normalize_tags(42) == ["42"]


def test_normalize_record_type_handles_all_and_unknown_values():
    assert normalize_record_type(None) is None
    assert normalize_record_type("all") is None
    assert normalize_record_type("SIP") is RecordType.SIP
    assert normalize_record_type(RecordType.LINKS) is RecordType.LINKS
    assert normalize_record_type("GHOST") is None


# ---------------------------------------------------------------------------
# Callback-логика поиска: фильтры, страницы, устаревшие кнопки
# ---------------------------------------------------------------------------


def test_filter_callbacks_cover_all_types_and_fit_telegram_limit():
    assert search_handlers.parse_filter_callback("search:filter:all") == "ALL"
    assert search_handlers.parse_filter_callback("search:filter:CREDENTIALS") == "CREDENTIALS"
    assert search_handlers.parse_filter_callback("search:filter:ghost") is None
    assert search_handlers.parse_filter_callback("search:filter:") is None
    for value in search_handlers.FILTERS:
        callback = f"search:filter:{value.lower()}"
        assert len(callback.encode()) <= TELEGRAM_CALLBACK_LIMIT


def test_page_callback_rejects_stale_and_negative_values():
    assert search_handlers.parse_page_callback("search:page:0") == 0
    assert search_handlers.parse_page_callback("search:page:7") == 7
    assert search_handlers.parse_page_callback("search:page:-1") is None
    assert search_handlers.parse_page_callback("search:page:abc") is None
    assert search_handlers.parse_page_callback("menu:list:x:ALL:d:0") is None


def test_stale_state_filters_and_pages_are_sanitized():
    assert search_handlers.normalize_filter("sip") == "SIP"
    assert search_handlers.normalize_filter("ghost") == "ALL"
    assert search_handlers.normalize_filter(None) == "ALL"
    assert search_handlers.normalize_page("3") == 3
    assert search_handlers.normalize_page("-4") == 0
    assert search_handlers.normalize_page(object()) == 0


def test_section_callback_parsing_and_section_search_button():
    assert search_handlers.parse_section_callback("search:section:holodka") == "holodka"
    assert search_handlers.parse_section_callback("search:section:") is None
    assert search_handlers.parse_section_callback("search:start") is None

    kb = menu.category_content_keyboard("holodka")
    callbacks = [button.callback_data for row in kb.inline_keyboard for button in row]
    assert "search:section:holodka" in callbacks
    assert callbacks[-1] == "menu:main"


def test_results_markup_has_visible_titles_pagination_and_filters():
    record = Record(title="Видимая запись", record_type=RecordType.LINKS, category_id=uuid4())
    record.id = uuid4()
    markup = search_handlers._results_markup([record], 12, 1, "LINKS")
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    texts = [button.text for row in markup.inline_keyboard for button in row]

    assert f"record:view:{record.id}" in callbacks
    assert "search:page:0" in callbacks
    assert "search:page:2" in callbacks
    assert "search:filter:links" in callbacks
    assert any("Видимая запись" in text for text in texts)


# ---------------------------------------------------------------------------
# Интеграционные тесты сервиса (нужен TEST_DATABASE_URL)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchFixture:
    session: AsyncSession
    user: User
    other: User
    open_category: Category
    closed_category: Category
    sub_category: Category
    records: dict[str, Record]
    tag: Tag
    file: File
    role: Role


@pytest.fixture
async def search_fixture():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated PostgreSQL test database")

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid4().hex[:10]
    created: dict[str, object] = {}
    session = session_factory()

    try:
        try:
            await session.execute(select(Permission.id).limit(1))
        except SQLAlchemyError:
            pytest.skip("Apply migrations to the test database first")

        permission = await session.scalar(
            select(Permission).where(Permission.code == PermissionCode.VIEW.value)
        )
        if permission is None:
            permission = Permission(code=PermissionCode.VIEW.value, name="Просмотр")
            session.add(permission)
            await session.flush()

        role = Role(code=f"SEARCH_ROLE_{suffix}", name=f"Search role {suffix}", is_system=False)
        session.add(role)
        await session.flush()
        session.add(RolePermission(role_id=role.id, permission_id=permission.id))

        user = User(
            telegram_id=900_000_000_000 + uuid4().int % 99_000_000_000,
            display_name="Search User",
            role=RoleCode.EMPLOYEE,
            role_id=role.id,
            status=UserStatus.ACTIVE,
        )
        other = User(
            telegram_id=900_000_000_000 + uuid4().int % 99_000_000_000,
            display_name="Search Other",
            role=RoleCode.EMPLOYEE,
            role_id=role.id,
            status=UserStatus.ACTIVE,
        )
        session.add_all([user, other])
        await session.flush()

        open_category = Category(
            code=f"search_open_{suffix}",
            name=f"Search Open {suffix}",
            category_type="root",
            status="ACTIVE",
        )
        closed_category = Category(
            code=f"search_closed_{suffix}",
            name=f"Search Closed {suffix}",
            category_type="root",
            status="ACTIVE",
        )
        session.add_all([open_category, closed_category])
        await session.flush()
        sub_category = Category(
            code=f"search_sub_{suffix}",
            name=f"Search Sub {suffix}",
            category_type="sub",
            parent_id=open_category.id,
            status="ACTIVE",
        )
        session.add(sub_category)
        await session.flush()

        session.add_all(
            [
                CategoryPermission(
                    category_id=open_category.id, access_mode=AccessMode.ALL_EMPLOYEES
                ),
                CategoryPermission(
                    category_id=closed_category.id, access_mode=AccessMode.PRIVATE
                ),
            ]
        )

        records = {
            "crm": Record(
                category_id=open_category.id,
                record_type=RecordType.CREDENTIALS,
                title="CRM Alpha Record",
                description="VIP clients and deals",
                created_by=other.id,
                created_at=datetime(2026, 1, 10, 12, 0, tzinfo=UTC),
            ),
            "sip": Record(
                category_id=sub_category.id,
                record_type=RecordType.SIP,
                title="SIP Beta Record",
                description="Телефония для отдела продаж",
                created_by=other.id,
                created_at=datetime(2026, 2, 10, 12, 0, tzinfo=UTC),
            ),
            "closed": Record(
                category_id=closed_category.id,
                record_type=RecordType.INSTRUCTIONS,
                title="CRM Closed Secret",
                created_by=other.id,
                created_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            ),
            "archived": Record(
                category_id=open_category.id,
                record_type=RecordType.OTHER,
                title="CRM Archived Record",
                status="ARCHIVED",
                created_by=other.id,
                created_at=datetime(2026, 1, 20, 12, 0, tzinfo=UTC),
            ),
            "private": Record(
                category_id=open_category.id,
                record_type=RecordType.OTHER,
                title="CRM Private Record",
                created_by=other.id,
                created_at=datetime(2026, 1, 25, 12, 0, tzinfo=UTC),
            ),
            "file": Record(
                category_id=open_category.id,
                record_type=RecordType.OTHER,
                title="Finance File Record",
                created_by=other.id,
                created_at=datetime(2026, 3, 10, 12, 0, tzinfo=UTC),
            ),
        }
        session.add_all(list(records.values()))
        await session.flush()

        session.add_all(
            [
                RecordField(
                    record_id=records["crm"].id,
                    field_key="login",
                    field_label="Логин",
                    field_type=FieldType.TEXT.value,
                    value_text="crm_user",
                ),
                RecordField(
                    record_id=records["crm"].id,
                    field_key="password",
                    field_label="Пароль",
                    field_type=FieldType.PASSWORD.value,
                    value_text="SuperSecret123",
                    encrypted_value="encrypted-placeholder",
                ),
                RecordField(
                    record_id=records["crm"].id,
                    field_key="url",
                    field_label="Ссылка",
                    field_type=FieldType.URL.value,
                    value_text="https://crm.example.com/alpha",
                ),
                RecordField(
                    record_id=records["sip"].id,
                    field_key="sip_username",
                    field_label="SIP Username",
                    field_type=FieldType.TEXT.value,
                    value_text="sip_user",
                ),
                RecordField(
                    record_id=records["sip"].id,
                    field_key="sip_password",
                    field_label="SIP Password",
                    field_type=FieldType.PASSWORD.value,
                    value_text="SipSecret",
                    encrypted_value="encrypted-placeholder",
                ),
            ]
        )
        tag = Tag(name=f"search_tag_{suffix}")
        session.add(tag)
        await session.flush()
        session.add(RecordTag(record_id=records["crm"].id, tag_id=tag.id))
        session.add(
            RecordPermission(record_id=records["private"].id, access_mode=AccessMode.PRIVATE)
        )

        file_row = File(
            original_name=f"report_finance_{suffix}.pdf",
            storage_key=f"test/search/{suffix}.pdf",
            bucket="test",
            mime_type="application/pdf",
            size=1024,
            uploaded_by=other.id,
        )
        session.add(file_row)
        await session.flush()
        session.add(RecordFile(record_id=records["file"].id, file_id=file_row.id))

        await session.commit()
        created = {
            "session": session,
            "user": user,
            "other": other,
            "open_category": open_category,
            "closed_category": closed_category,
            "sub_category": sub_category,
            "records": records,
            "tag": tag,
            "file": file_row,
            "role": role,
        }
        yield SearchFixture(**created)
    finally:
        try:
            if created:
                record_ids = [record.id for record in created["records"].values()]
                await session.execute(delete(Record).where(Record.id.in_(record_ids)))
                await session.execute(delete(File).where(File.id == created["file"].id))
                await session.execute(delete(Tag).where(Tag.id == created["tag"].id))
                await session.execute(
                    delete(CategoryPermission).where(
                        CategoryPermission.category_id.in_(
                            [
                                created["open_category"].id,
                                created["closed_category"].id,
                            ]
                        )
                    )
                )
                await session.execute(
                    delete(Category).where(Category.id == created["sub_category"].id)
                )
                await session.execute(
                    delete(Category).where(
                        Category.id.in_(
                            [
                                created["open_category"].id,
                                created["closed_category"].id,
                            ]
                        )
                    )
                )
                await session.execute(
                    delete(RolePermission).where(
                        RolePermission.role_id == created["role"].id
                    )
                )
                await session.execute(
                    delete(User).where(
                        User.id.in_([created["user"].id, created["other"].id])
                    )
                )
                await session.execute(delete(Role).where(Role.id == created["role"].id))
                await session.commit()
        except SQLAlchemyError:
            await session.rollback()
        finally:
            await session.close()
            await engine.dispose()


def _titles(records: list[Record]) -> set[str]:
    return {record.title for record in records}


async def test_search_never_returns_closed_archived_or_private_records(search_fixture):
    fixture = search_fixture
    found = await search_records(fixture.session, "CRM", fixture.user, limit=50)
    titles = _titles(found)

    assert "CRM Alpha Record" in titles
    # Названия закрытых записей не раскрываются
    assert "CRM Closed Secret" not in titles
    assert "CRM Archived Record" not in titles
    assert "CRM Private Record" not in titles


async def test_search_is_case_insensitive_partial_and_multiword(search_fixture):
    fixture = search_fixture
    partial = await search_records(fixture.session, "alpha", fixture.user)
    assert _titles(partial) == {"CRM Alpha Record"}

    multiword = await search_records(fixture.session, "CRM vip", fixture.user)
    assert _titles(multiword) == {"CRM Alpha Record"}

    # Второе слово не найдено — записи быть не должно
    assert await search_records(fixture.session, "CRM missing", fixture.user) == []

    exact = await search_records(
        fixture.session, "CRM Alpha Record", fixture.user, exact=True
    )
    assert _titles(exact) == {"CRM Alpha Record"}
    # Точный поиск не должен срабатывать на часть названия
    assert await search_records(fixture.session, "CRM Alpha", fixture.user, exact=True) == []


async def test_search_by_fields_tags_files_and_record_id(search_fixture):
    fixture = search_fixture

    assert _titles(
        await search_records(fixture.session, "crm_user", fixture.user)
    ) == {"CRM Alpha Record"}
    assert _titles(
        await search_records(fixture.session, "sip_user", fixture.user)
    ) == {"SIP Beta Record"}
    assert _titles(
        await search_records(fixture.session, fixture.tag.name, fixture.user)
    ) == {"CRM Alpha Record"}
    assert _titles(
        await search_records(fixture.session, "finance_", fixture.user)
    ) == {"Finance File Record"}

    record = fixture.records["crm"]
    by_id = await search_records(fixture.session, str(record.id)[:8], fixture.user)
    assert _titles(by_id) == {"CRM Alpha Record"}
    by_full_id = await search_records(fixture.session, str(record.id), fixture.user)
    assert _titles(by_full_id) == {"CRM Alpha Record"}


async def test_search_does_not_index_passwords_and_secrets(search_fixture):
    fixture = search_fixture
    assert await search_records(fixture.session, "SuperSecret123", fixture.user) == []
    assert await search_records(fixture.session, "SipSecret", fixture.user) == []


async def test_search_filters_by_category_type_tags_and_dates(search_fixture):
    fixture = search_fixture

    in_open = await search_records(
        fixture.session, "Record", fixture.user, category_code=fixture.open_category.code
    )
    assert _titles(in_open) == {
        "CRM Alpha Record",
        "SIP Beta Record",
        "Finance File Record",
    }
    assert "CRM Closed Secret" not in _titles(in_open)

    # Поиск в подкатегории не захватывает родительские записи
    in_sub = await search_records(
        fixture.session, "Record", fixture.user, category_code=fixture.sub_category.code
    )
    assert _titles(in_sub) == {"SIP Beta Record"}

    by_type = await search_records(
        fixture.session, "Record", fixture.user, record_type=RecordType.SIP
    )
    assert _titles(by_type) == {"SIP Beta Record"}

    by_type_str = await search_records(
        fixture.session, "Record", fixture.user, record_type="CREDENTIALS"
    )
    assert _titles(by_type_str) == {"CRM Alpha Record"}

    by_tag = await search_records(
        fixture.session, "Record", fixture.user, tags=[fixture.tag.name]
    )
    assert _titles(by_tag) == {"CRM Alpha Record"}

    by_date = await search_records(
        fixture.session,
        "Record",
        fixture.user,
        date_from=date(2026, 2, 1),
        date_to=date(2026, 2, 28),
    )
    assert _titles(by_date) == {"SIP Beta Record"}


async def test_search_respects_limit_after_permission_filtering(search_fixture):
    fixture = search_fixture
    found = await search_records(fixture.session, "CRM", fixture.user, limit=1)
    assert len(found) == 1
    assert found[0].title == "CRM Alpha Record"


async def test_search_by_type_title_and_url(search_fixture):
    fixture = search_fixture
    by_type_title = await search_records(fixture.session, "телефония", fixture.user)
    assert "SIP Beta Record" in _titles(by_type_title)

    by_url = await search_records(fixture.session, "crm.example.com", fixture.user)
    assert _titles(by_url) == {"CRM Alpha Record"}


async def test_search_returns_nothing_for_unknown_category_or_bad_type(search_fixture):
    fixture = search_fixture
    assert (
        await search_records(
            fixture.session, "Record", fixture.user, category_code="no_such_category"
        )
        == []
    )
    assert (
        await search_records(fixture.session, "Record", fixture.user, record_type="GHOST")
        == []
    )


async def test_closed_record_cannot_be_found_by_exact_title(search_fixture):
    fixture = search_fixture
    found = await search_records(
        fixture.session, "CRM Closed Secret", fixture.user, exact=True
    )
    assert found == []
