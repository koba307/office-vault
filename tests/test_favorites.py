"""Тесты избранного Phase 07 с повторной проверкой доступа.

Чистые тесты не требуют базы. Интеграционные используют TEST_DATABASE_URL
(изолированная PostgreSQL с применёнными миграциями) и пропускаются без неё.
"""

import inspect
import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.handlers import favorites as favorites_handlers
from app.bot.handlers import records as records_handlers
from app.bot.handlers.favorites import STALE_TEXT, favorites_view
from app.bot.keyboards.pagination import pagination_keyboard
from app.database.models import (
    Category,
    Favorite,
    Record,
    RecordField,
    RecordFile,
    RecordType,
    RoleCode,
    User,
)
from app.services.favorites import service as favorites_service
from app.services.favorites.service import (
    FavoriteError,
    FavoriteItem,
    add_favorite,
    is_favorite,
    list_favorites,
    remove_favorite,
)
from app.services.permissions.errors import DenyReason

# ---------------------------------------------------------------------------
# Чистые тесты: API сервиса и обработчиков
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["add_favorite", "remove_favorite", "list_favorites", "is_favorite"])
def test_favorites_service_api(name):
    function = getattr(favorites_service, name)
    assert inspect.iscoroutinefunction(function)
    # Соглашение проекта: session — первый positional-аргумент.
    assert next(iter(inspect.signature(function).parameters)) == "session"


def test_list_favorites_paging_is_keyword_only():
    parameters = inspect.signature(list_favorites).parameters
    assert parameters["limit"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["offset"].kind is inspect.Parameter.KEYWORD_ONLY


def test_favorite_error_is_value_error():
    assert issubclass(FavoriteError, ValueError)


def test_favorites_router_exposes_handlers():
    names = {
        handler.callback.__name__ for handler in favorites_handlers.router.callback_query.handlers
    }
    assert {"add_fav", "remove_fav", "list_fav", "list_fav_page"} <= names


def test_favorites_router_is_registered_before_stale_router():
    from pathlib import Path

    source = Path("app/bot/main.py").read_text(encoding="utf-8")
    assert source.index("dp.include_router(favorites.router)") < source.index(
        "dp.include_router(navigation.stale_router)"
    )


def _callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.from_user.id = 1
    return callback


@pytest.mark.parametrize(
    "data",
    ["favorite:add:not-a-uuid", "favorite:add:", "favorite:remove:123"],
)
async def test_add_and_remove_reject_stale_record_id(data):
    callback = _callback(data)
    handler = (
        favorites_handlers.add_fav if data.startswith("favorite:add") else favorites_handlers.remove_fav
    )

    await handler(callback)

    callback.answer.assert_awaited_once_with(STALE_TEXT, show_alert=True)
    callback.message.edit_text.assert_not_called()


@pytest.mark.parametrize("data", ["favorites:page:abc", "favorites:page:-1", "favorites:page:"])
async def test_list_page_rejects_invalid_page(data):
    callback = _callback(data)

    await favorites_handlers.list_fav_page(callback)

    callback.answer.assert_awaited_once_with(STALE_TEXT, show_alert=True)
    callback.message.edit_text.assert_not_called()


def _item(title="Пароль", category="Холодка", record_type=RecordType.CREDENTIALS, added=None):
    record = Record(
        id=uuid4(),
        category_id=uuid4(),
        record_type=record_type,
        title=title,
        status="ACTIVE",
        created_by=uuid4(),
    )
    return FavoriteItem(
        record=record,
        category_name=category,
        added_at=added or datetime(2026, 11, 15, 10, 30, tzinfo=UTC),
    )


def test_favorites_view_empty_list():
    text, markup = favorites_view([], 0, 0)

    assert text == "⭐ Избранное пусто"
    assert [button.callback_data for row in markup.inline_keyboard for button in row] == [
        "menu:main"
    ]


def test_favorites_view_shows_title_category_type_and_date():
    item = _item(title="<Почта>", category="Холодка")
    text, markup = favorites_view([item], 1, 0)

    assert "<b>&lt;Почта&gt;</b>" in text
    assert "Раздел: Холодка" in text
    assert "Тип:" in text
    assert "добавлено: 15.11.2026" in text
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert f"record:view:{item.record.id}" in callbacks


def test_favorites_view_shows_dash_when_date_unknown():
    item = _item()
    item = FavoriteItem(record=item.record, category_name="Раздел", added_at=None)
    text, _ = favorites_view([item], 1, 0)
    assert "добавлено: —" in text


def test_favorites_view_pagination_uses_page_callbacks():
    items = [_item(title=f"Запись {n}") for n in range(10)]
    text, markup = favorites_view(items, 25, 1)

    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert "favorites:page:0" in callbacks
    assert "favorites:page:2" in callbacks
    assert "стр. 2/3" in text
    assert callbacks[-1] == "menu:main"


def test_favorites_view_has_no_pagination_for_single_page():
    _, markup = favorites_view([_item()], 1, 0)
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert not any(cb.startswith("favorites:page:") for cb in callbacks)


def test_pagination_keyboard_matches_favorites_page_prefix():
    markup = pagination_keyboard(25, 0, 10, "favorites:page")
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert callbacks and all(cb.startswith("favorites:page:") for cb in callbacks)


def test_favorite_button_toggles_action():
    rid = uuid4()

    add = records_handlers._favorite_button(rid, False)
    remove = records_handlers._favorite_button(rid, True)

    assert add.callback_data == f"favorite:add:{rid}"
    assert remove.callback_data == f"favorite:remove:{rid}"
    assert add.text == "⭐ В избранное"
    assert remove.text == "💔 Убрать из избранного"
    assert len(add.callback_data.encode()) <= 64
    assert len(remove.callback_data.encode()) <= 64


# ---------------------------------------------------------------------------
# Интеграционные тесты (нужен TEST_DATABASE_URL)
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


async def _superadmin(session):
    """Суперадмин видит любую категорию, поэтому тесты не зависят от настроек прав."""
    return (
        await session.execute(select(User).where(User.role == RoleCode.SUPERADMIN).limit(1))
    ).scalar_one_or_none()


async def _make_category(session):
    category = Category(
        code=f"fav_{uuid4().hex[:10]}",
        name="Избранное",
        category_type="root",
        status="ACTIVE",
        sort_order=99,
    )
    session.add(category)
    await session.commit()
    await session.refresh(category)
    return category


async def _make_record(session, user, category, *, title="Избранная запись", status="ACTIVE"):
    record = Record(
        category_id=category.id,
        record_type=RecordType.OTHER,
        title=title,
        description=None,
        status=status,
        created_by=user.id,
    )
    session.add(record)
    await session.flush()
    session.add(
        RecordField(
            record_id=record.id,
            field_key="note",
            field_label="Note",
            field_type="TEXT",
            value_text="значение",
            sort_order=0,
        )
    )
    await session.commit()
    await session.refresh(record)
    return record


async def _cleanup(session, record_ids, category_ids):
    for record_id in record_ids:
        await session.execute(delete(Favorite).where(Favorite.record_id == record_id))
        await session.execute(delete(RecordField).where(RecordField.record_id == record_id))
        await session.execute(delete(RecordFile).where(RecordFile.record_id == record_id))
        await session.execute(delete(Record).where(Record.id == record_id))
    if category_ids:
        await session.execute(delete(Category).where(Category.id.in_(list(category_ids))))
    await session.commit()


async def test_add_remove_and_is_favorite(db_session):
    user = await _superadmin(db_session)
    if user is None:
        pytest.skip("The test database has no superadmin")
    category = await _make_category(db_session)
    record = await _make_record(db_session, user, category)

    try:
        assert await is_favorite(db_session, user.id, record.id) is False
        assert await add_favorite(db_session, user.id, record.id) is True
        assert await add_favorite(db_session, user.id, record.id) is False
        assert await is_favorite(db_session, user.id, record.id) is True

        assert await remove_favorite(db_session, user.id, record.id) is True
        assert await remove_favorite(db_session, user.id, record.id) is False
        assert await is_favorite(db_session, user.id, record.id) is False
    finally:
        await _cleanup(db_session, [record.id], [category.id])


async def test_add_rejects_missing_and_archived_records(db_session):
    user = await _superadmin(db_session)
    if user is None:
        pytest.skip("The test database has no superadmin")
    category = await _make_category(db_session)
    archived = await _make_record(db_session, user, category, status="ARCHIVED")

    try:
        with pytest.raises(FavoriteError):
            await add_favorite(db_session, user.id, uuid4())
        with pytest.raises(FavoriteError):
            await add_favorite(db_session, user.id, archived.id)
        assert await is_favorite(db_session, user.id, archived.id) is False
    finally:
        await _cleanup(db_session, [archived.id], [category.id])


async def test_list_favorites_paginates(db_session):
    user = await _superadmin(db_session)
    if user is None:
        pytest.skip("The test database has no superadmin")
    category = await _make_category(db_session)
    records = [
        await _make_record(db_session, user, category, title=f"Запись {n}") for n in range(3)
    ]
    for record in records:
        await add_favorite(db_session, user.id, record.id)

    try:
        first, total = await list_favorites(db_session, user.id, limit=2, offset=0)
        assert total == 3
        assert len(first) == 2
        assert all(item.category_name == "Избранное" for item in first)
        assert all(item.added_at is not None for item in first)

        last, total = await list_favorites(db_session, user.id, limit=2, offset=2)
        assert total == 3
        assert len(last) == 1
    finally:
        await _cleanup(db_session, [r.id for r in records], [category.id])


async def test_revoked_access_hides_favorite_but_keeps_row(db_session, monkeypatch):
    """Отзыв доступа убирает запись из выдачи; строка избранного сохраняется."""
    user = await _superadmin(db_session)
    if user is None:
        pytest.skip("The test database has no superadmin")
    category = await _make_category(db_session)
    record = await _make_record(db_session, user, category)
    await add_favorite(db_session, user.id, record.id)

    original = favorites_service.record_denial_reason

    async def revoked(ctx, record_id, action, session):
        if record_id == record.id:
            return DenyReason.RECORD_DENIED
        return await original(ctx, record_id, action, session)

    try:
        monkeypatch.setattr(favorites_service, "record_denial_reason", revoked)
        items, total = await list_favorites(db_session, user.id, limit=10, offset=0)
        assert total == 0
        assert items == []
        assert await is_favorite(db_session, user.id, record.id) is True

        monkeypatch.setattr(favorites_service, "record_denial_reason", original)
        items, total = await list_favorites(db_session, user.id, limit=10, offset=0)
        assert total == 1
        assert items[0].record.id == record.id
    finally:
        await _cleanup(db_session, [record.id], [category.id])


async def test_deleted_record_is_excluded_from_list(db_session):
    user = await _superadmin(db_session)
    if user is None:
        pytest.skip("The test database has no superadmin")
    category = await _make_category(db_session)
    record = await _make_record(db_session, user, category)
    await add_favorite(db_session, user.id, record.id)

    try:
        record.status = "DELETED"
        await db_session.commit()

        items, total = await list_favorites(db_session, user.id, limit=10, offset=0)
        assert total == 0
        assert items == []
    finally:
        await _cleanup(db_session, [record.id], [category.id])


async def test_add_denied_when_access_revoked(db_session, monkeypatch):
    user = await _superadmin(db_session)
    if user is None:
        pytest.skip("The test database has no superadmin")
    category = await _make_category(db_session)
    record = await _make_record(db_session, user, category)

    async def denied(*_args, **_kwargs):
        return False

    try:
        monkeypatch.setattr(favorites_service, "check_record_permission", denied)
        with pytest.raises(FavoriteError):
            await add_favorite(db_session, user.id, record.id)
        assert await is_favorite(db_session, user.id, record.id) is False
    finally:
        await _cleanup(db_session, [record.id], [category.id])

