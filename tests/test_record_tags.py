import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.models import Category, Record, RecordType, Tag, User
from app.services.categories import create_category
from app.services.records.tags import (
    TAG_KEY_LENGTH,
    TagConflict,
    TagError,
    TagNotFound,
    TagValidationError,
    add_tag_to_record,
    create_tag,
    get_record_tags,
    get_tag_by_key,
    list_tags,
    remove_tag_from_record,
    tag_key,
    validate_tag_color,
    validate_tag_name,
)
from app.services.records.versions import RecordNotFound

TELEGRAM_CALLBACK_LIMIT = 64


# ---------------------------------------------------------------------------
# Чистые тесты: валидация и ключи (не требуют БД)
# ---------------------------------------------------------------------------


def test_validate_tag_name_normalizes_spaces_and_case():
    assert validate_tag_name("  Срочно   ВАЖНО ") == "срочно важно"


@pytest.mark.parametrize("name", ["", "   ", "x" * 65])
def test_validate_tag_name_rejects_invalid_values(name):
    with pytest.raises(TagValidationError):
        validate_tag_name(name)


def test_validate_tag_color_accepts_hex_and_optional_values():
    assert validate_tag_color(None) is None
    assert validate_tag_color("   ") is None
    assert validate_tag_color("#FF8800") == "#ff8800"


@pytest.mark.parametrize("color", ["red", "#FFF", "#GGGGGG", "FF8800"])
def test_validate_tag_color_rejects_invalid_values(color):
    with pytest.raises(TagValidationError):
        validate_tag_color(color)


def test_tag_key_is_short_and_fits_callback_data():
    tag = Tag(name="тест")
    tag.id = uuid4()
    key = tag_key(tag)

    assert len(key) == TAG_KEY_LENGTH
    callback = f"record:tagdel:{uuid4()}:{key}"
    assert len(callback.encode()) <= TELEGRAM_CALLBACK_LIMIT


def test_tag_errors_are_value_errors():
    for error in (TagNotFound, TagConflict, TagValidationError):
        assert issubclass(error, TagError)
        assert issubclass(error, ValueError)


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
                await session.execute(select(Tag.id).limit(1))
            except SQLAlchemyError:
                pytest.skip("Apply migrations to the test database first")
            yield session
    finally:
        await engine.dispose()


async def test_tag_lifecycle_on_record(db_session):
    user = (await db_session.execute(select(User).limit(1))).scalar_one_or_none()
    if user is None:
        pytest.skip("The test database has no users")

    category = await create_category(
        db_session, f"tag_{uuid4().hex[:8]}", "Тест тегов", category_type="root"
    )
    record = Record(
        category_id=category.id,
        record_type=RecordType.OTHER,
        title="С тегами",
        created_by=user.id,
    )
    db_session.add(record)
    await db_session.commit()
    record_id = record.id
    category_id = category.id
    tag_name = f"test_tag_{uuid4().hex[:8]}"
    tag_id = None
    try:
        tag = await create_tag(tag_name.upper(), "#FF8800", db_session)
        tag_id = tag.id
        key = tag_key(tag)
        assert tag.name == tag_name
        assert tag.color == "#ff8800"

        # Конфликт делает rollback, поэтому значения берём заранее.
        with pytest.raises(TagConflict):
            await create_tag(tag_name, None, db_session)
        assert tag_id in {item.id for item in await list_tags(db_session)}

        assert await add_tag_to_record(record_id, tag_id, db_session) is True
        assert await add_tag_to_record(record_id, tag_id, db_session) is False
        assert [item.id for item in await get_record_tags(record_id, db_session)] == [tag_id]
        assert (await get_tag_by_key(key, db_session)).id == tag_id

        assert await remove_tag_from_record(record_id, tag_id, db_session) is True
        assert await remove_tag_from_record(record_id, tag_id, db_session) is False
        assert await get_record_tags(record_id, db_session) == []

        with pytest.raises(TagNotFound):
            await add_tag_to_record(record_id, uuid4(), db_session)
        with pytest.raises(RecordNotFound):
            await add_tag_to_record(uuid4(), tag_id, db_session)
    finally:
        if tag_id is not None:
            await db_session.execute(delete(Tag).where(Tag.id == tag_id))
            await db_session.commit()
        await db_session.execute(delete(Record).where(Record.id == record_id))
        await db_session.execute(delete(Category).where(Category.id == category_id))
        await db_session.commit()
