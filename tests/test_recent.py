"""Тесты истории просмотров (Phase 07).

Чистые тесты проверяют API сервиса, разбор callback и рендер экрана без базы.
Интеграционные используют TEST_DATABASE_URL (изолированная PostgreSQL с
применёнными миграциями) и проверяют upsert, пагинацию, пересчёт прав,
лимит истории и то, что секреты в истории не сохраняются.
"""

import inspect
import os
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.handlers import recent as recent_handlers
from app.bot.handlers import records as records_handlers
from app.bot.handlers.recent import (
    CLEAR_CALLBACK,
    LIST_CALLBACK,
    PAGE_PREFIX,
    PAGE_SIZE,
    build_recent_rows,
    format_recent_text,
    parse_page_callback,
)
from app.core.encryption import encrypt_secret
from app.database.models import (
    Category,
    RecentRecord,
    Record,
    RecordField,
    RecordType,
    RoleCode,
    User,
)
from app.services.permissions import PermissionService
from app.services.recent import service as recent_service
from app.services.recent.service import (
    RECENT_DEFAULT_LIMIT,
    RecentEntry,
    add_recent,
    clear_recent,
    list_recent,
)

# ---------------------------------------------------------------------------
# Чистые тесты: API сервиса (не требуют БД)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["add_recent", "list_recent", "clear_recent"])
def test_recent_service_api_exists(name):
    function = getattr(recent_service, name)
    assert inspect.iscoroutinefunction(function)
    # Соглашение проекта: session — первый positional-аргумент.
    parameters = list(inspect.signature(function).parameters)
    assert parameters[0] == "session"


def test_list_recent_pagination_is_keyword_only():
    parameters = inspect.signature(list_recent).parameters
    for name in ("limit", "offset"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["limit"].default == RECENT_DEFAULT_LIMIT
    assert parameters["offset"].default == 0


def test_recent_history_schema_has_no_secret_columns():
    # В истории только связь и дата: колонок для значений/секретов нет.
    columns = {column.name for column in RecentRecord.__table__.columns}
    assert columns == {"user_id", "record_id", "viewed_at"}


def test_recent_entry_exposes_only_safe_fields():
    names = {field.name for field in dataclass_fields(RecentEntry)}
    assert names == {"record_id", "title", "category_name", "record_type", "viewed_at"}


def test_recent_service_never_touches_field_values():
    source = inspect.getsource(recent_service)
    for forbidden in ("value_text", "encrypted_value", "decrypt_secret", "RecordField"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# Чистые тесты: разбор callback и рендер экрана
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("recent:page:0", 0),
        ("recent:page:7", 7),
        # Устаревшие и битые кнопки:
        ("recent:page:", None),
        ("recent:page:two", None),
        ("recent:page:-1", None),
        ("recent:page:1.5", None),
        ("recent:page:1:2", None),
        ("recent:list", None),
        ("menu:list:general:ALL:d:0", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_page_callback(data, expected):
    assert parse_page_callback(data) == expected


def _entry(index: int = 0, **overrides) -> RecentEntry:
    defaults = {
        "record_id": uuid4(),
        "title": f"Запись {index}",
        "category_name": "Холодка",
        "record_type": RecordType.CREDENTIALS.value,
        "viewed_at": datetime(2026, 11, 15, 10, 30, tzinfo=UTC),
    }
    defaults.update(overrides)
    return RecentEntry(**defaults)


def test_build_recent_rows_buttons_and_callbacks():
    entries = [_entry(1), _entry(2)]
    rows = build_recent_rows(entries, total=len(entries), page=0)

    view_callbacks = [
        button.callback_data
        for row in rows
        for button in row
        if (button.callback_data or "").startswith("record:view:")
    ]
    assert view_callbacks == [f"record:view:{entry.record_id}" for entry in entries]

    texts = [button.text for row in rows for button in row]
    assert "🧹 Очистить историю" in texts
    assert any((button.callback_data or "") == CLEAR_CALLBACK for row in rows for button in row)
    assert any((button.callback_data or "") == "menu:main" for row in rows for button in row)


def test_build_recent_rows_pagination_and_clear_hidden():
    entries = [_entry(index) for index in range(PAGE_SIZE)]
    rows = build_recent_rows(entries, total=PAGE_SIZE * 3, page=1)
    pagination_callbacks = [
        button.callback_data
        for row in rows
        for button in row
        if (button.callback_data or "").startswith(PAGE_PREFIX)
    ]
    assert pagination_callbacks, "при трёх страницах пагинация обязательна"
    assert all((data or "").startswith(PAGE_PREFIX) for data in pagination_callbacks)

    # Пустая история: кнопки очистки нет
    empty_rows = build_recent_rows([], total=0, page=0)
    empty_callbacks = [button.callback_data for row in empty_rows for button in row]
    assert CLEAR_CALLBACK not in empty_callbacks


def test_format_recent_text_shows_title_category_type_date():
    entry = _entry(1, viewed_at=datetime(2026, 11, 15, 10, 30, tzinfo=UTC))
    text = format_recent_text([entry], page=0, pages=1)
    assert entry.title in text
    assert entry.category_name in text
    assert "Логины и пароли" in text
    assert "15.11.2026 10:30" in text

    empty = format_recent_text([], page=0, pages=1)
    assert "История пока пуста" in empty


def test_format_recent_text_escapes_html_and_hides_values():
    secret = "top-secret-value"
    entry = _entry(1, title=f"<b>{secret}</b>")
    text = format_recent_text([entry], page=0, pages=1)
    assert "<b>&lt;b&gt;" in text
    assert f"<b>{secret}</b>" not in text


def test_records_handlers_recent_wiring():
    # Карточка записи фиксирует просмотр...
    card_source = inspect.getsource(records_handlers.render_record_card)
    assert "add_recent(" in card_source
    # ...а раскрытие секретов к истории не обращается и значения не сохраняет.
    for name in ("reveal_secret", "reveal_secret_short"):
        source = inspect.getsource(getattr(records_handlers, name))
        assert "add_recent" not in source


# ---------------------------------------------------------------------------
# Интеграционные тесты: PostgreSQL (TEST_DATABASE_URL)
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


async def _superadmin(session: AsyncSession) -> User:
    user = (
        await session.execute(select(User).where(User.role == RoleCode.SUPERADMIN).limit(1))
    ).scalar_one_or_none()
    if user is None:
        pytest.skip("The test database has no seeded SUPERADMIN user")
    return user


async def _make_record(session: AsyncSession, user: User, title: str) -> tuple[Record, Category]:
    category = Category(
        code=f"rc_{uuid4().hex[:10]}",
        name="Recent Test",
        category_type="root",
        status="ACTIVE",
        sort_order=99,
    )
    session.add(category)
    await session.flush()
    record = Record(
        category_id=category.id,
        record_type=RecordType.OTHER,
        title=title,
        status="ACTIVE",
        created_by=user.id,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record, category


async def _drop_records(session: AsyncSession, user_id, record_ids, category_ids) -> None:
    if record_ids:
        await session.execute(
            delete(RecentRecord).where(
                RecentRecord.user_id == user_id,
                RecentRecord.record_id.in_(list(record_ids)),
            )
        )
        await session.execute(delete(RecordField).where(RecordField.record_id.in_(list(record_ids))))
        await session.execute(delete(Record).where(Record.id.in_(list(record_ids))))
    if category_ids:
        await session.execute(delete(Category).where(Category.id.in_(list(category_ids))))
    await session.commit()


async def test_add_recent_upserts_single_row(db_session):
    user = await _superadmin(db_session)
    record, category = await _make_record(db_session, user, "Upsert")
    try:
        await add_recent(db_session, user.id, record.id)
        first = (
            await db_session.execute(
                select(RecentRecord).where(
                    RecentRecord.user_id == user.id, RecentRecord.record_id == record.id
                )
            )
        ).scalar_one()
        first_viewed_at = first.viewed_at

        await add_recent(db_session, user.id, record.id)
        rows = (
            await db_session.execute(
                select(RecentRecord).where(
                    RecentRecord.user_id == user.id, RecentRecord.record_id == record.id
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].viewed_at >= first_viewed_at
    finally:
        await _drop_records(db_session, user.id, [record.id], [category.id])


async def test_list_recent_ordering_pagination_and_total(db_session):
    user = await _superadmin(db_session)
    made = [await _make_record(db_session, user, f"Record {index}") for index in range(3)]
    records = [record for record, _ in made]
    categories = [category for _, category in made]
    viewed_at = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        for index, record in enumerate(records):
            await add_recent(db_session, user.id, record.id)
            row = (
                await db_session.execute(
                    select(RecentRecord).where(
                        RecentRecord.user_id == user.id, RecentRecord.record_id == record.id
                    )
                )
            ).scalar_one()
            row.viewed_at = viewed_at + timedelta(minutes=index)
        await db_session.commit()

        page1, total = await list_recent(db_session, user.id, limit=2, offset=0)
        assert total == 3
        assert [entry.record_id for entry in page1] == [records[2].id, records[1].id]

        page2, total2 = await list_recent(db_session, user.id, limit=2, offset=2)
        assert total2 == 3
        assert [entry.record_id for entry in page2] == [records[0].id]

        entry = page1[0]
        assert entry.title == "Record 2"
        assert entry.category_name == "Recent Test"
        assert entry.record_type == RecordType.OTHER.value
        assert entry.viewed_at == viewed_at + timedelta(minutes=2)
    finally:
        await _drop_records(db_session, user.id, [record.id for record in records],
                            [category.id for category in categories])


async def test_list_recent_excludes_records_with_changed_permissions(db_session, monkeypatch):
    user = await _superadmin(db_session)
    allowed, category_a = await _make_record(db_session, user, "Allowed")
    blocked, category_b = await _make_record(db_session, user, "Blocked")
    archived, category_c = await _make_record(db_session, user, "Archived")
    original_can = PermissionService.can

    async def fake_can(user_arg, action, target_type, target_id, session):
        if target_id == blocked.id:
            return False  # имитируем отобранные права
        return await original_can(user_arg, action, target_type, target_id, session)

    monkeypatch.setattr(PermissionService, "can", staticmethod(fake_can))
    try:
        for record in (allowed, blocked, archived):
            await add_recent(db_session, user.id, record.id)

        archived.status = "ARCHIVED"
        await db_session.commit()

        entries, total = await list_recent(db_session, user.id, limit=10, offset=0)
        visible_ids = {entry.record_id for entry in entries}
        assert visible_ids == {allowed.id}
        assert total == 1
    finally:
        await _drop_records(
            db_session,
            user.id,
            [allowed.id, blocked.id, archived.id],
            [category_a.id, category_b.id, category_c.id],
        )


async def test_list_recent_excludes_records_of_inactive_category(db_session):
    user = await _superadmin(db_session)
    record, category = await _make_record(db_session, user, "Hidden section")
    try:
        await add_recent(db_session, user.id, record.id)
        category.status = "ARCHIVED"
        await db_session.commit()

        entries, total = await list_recent(db_session, user.id, limit=10, offset=0)
        assert entries == []
        assert total == 0
    finally:
        await _drop_records(db_session, user.id, [record.id], [category.id])


async def test_clear_recent_removes_all_rows(db_session):
    user = await _superadmin(db_session)
    first, category_a = await _make_record(db_session, user, "First")
    second, category_b = await _make_record(db_session, user, "Second")
    try:
        await add_recent(db_session, user.id, first.id)
        await add_recent(db_session, user.id, second.id)

        removed = await clear_recent(db_session, user.id)
        assert removed >= 2
        entries, total = await list_recent(db_session, user.id, limit=10, offset=0)
        assert entries == []
        assert total == 0
        # Повторная очистка ничего не удаляет
        assert await clear_recent(db_session, user.id) == 0
    finally:
        # Очищаем строки истории без привязки к id: они уже удалены clear_recent
        await db_session.execute(delete(RecentRecord).where(RecentRecord.user_id == user.id))
        await _drop_records(db_session, user.id, [first.id, second.id], [category_a.id, category_b.id])


async def test_add_recent_trims_history_to_limit(db_session, monkeypatch):
    user = await _superadmin(db_session)
    monkeypatch.setattr(recent_service, "RECENT_HISTORY_LIMIT", 5)
    made = [await _make_record(db_session, user, f"Trim {index}") for index in range(7)]
    records = [record for record, _ in made]
    categories = [category for _, category in made]
    base = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        for index, record in enumerate(records):
            await add_recent(db_session, user.id, record.id)
            row = (
                await db_session.execute(
                    select(RecentRecord).where(
                        RecentRecord.user_id == user.id, RecentRecord.record_id == record.id
                    )
                )
            ).scalar_one()
            # Детерминированные метки: самые свежие — последние три записи
            row.viewed_at = base + timedelta(minutes=index)
            await db_session.commit()

        entries, total = await list_recent(db_session, user.id, limit=10, offset=0)
        assert total == 5
        assert {entry.record_id for entry in entries} == {record.id for record in records[2:]}
    finally:
        await _drop_records(db_session, user.id, [record.id for record in records],
                            [category.id for category in categories])


async def test_history_never_contains_secrets(db_session):
    user = await _superadmin(db_session)
    record, category = await _make_record(db_session, user, "Secret holder")
    plain = "sup3r-s3cr3t"
    encrypted = encrypt_secret(plain)
    try:
        db_session.add(
            RecordField(
                record_id=record.id,
                field_key="password",
                field_label="Пароль",
                field_type="PASSWORD",
                encrypted_value=encrypted,
                sort_order=0,
            )
        )
        await db_session.commit()

        await add_recent(db_session, user.id, record.id)
        rows = (
            await db_session.execute(
                select(RecentRecord).where(RecentRecord.record_id == record.id)
            )
        ).scalars().all()
        assert rows
        # В строке истории нет значения и шифртекста — только связь и дата
        for row in rows:
            values = [getattr(row, column.name) for column in RecentRecord.__table__.columns]
            assert all(plain not in str(value) and encrypted not in str(value) for value in values)

        entries, total = await list_recent(db_session, user.id, limit=10, offset=0)
        assert total == 1
        text = format_recent_text(entries, page=0, pages=1)
        assert plain not in text
        assert encrypted not in text
    finally:
        await _drop_records(db_session, user.id, [record.id], [category.id])


def test_recent_handler_callback_constants():
    # Точные callback-адреса экрана: список, страницы и очистка с подтверждением
    assert recent_handlers.LIST_CALLBACK == "recent:list"
    assert recent_handlers.PAGE_PREFIX == "recent:page:"
    assert recent_handlers.CLEAR_CALLBACK == "recent:clear"
    assert recent_handlers.CLEAR_YES_CALLBACK == "recent:clear:yes"
    assert recent_handlers.CLEAR_NO_CALLBACK == "recent:clear:no"
    assert LIST_CALLBACK == recent_handlers.LIST_CALLBACK
