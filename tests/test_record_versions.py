import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.handlers import records as records_handlers
from app.core.encryption import decrypt_secret, encrypt_secret
from app.database.models import Category, Record, RecordField, RecordType, User
from app.services.categories import create_category
from app.services.records.versions import (
    RecordNotFound,
    RecordVersionError,
    VersionConflict,
    VersionExists,
    VersionNotFound,
    build_snapshot,
    compare_snapshots,
    create_version,
    get_version_diff,
    list_versions,
    restore_version,
    update_record_with_lock,
)

TELEGRAM_CALLBACK_LIMIT = 64


# ---------------------------------------------------------------------------
# Чистые тесты: снимки, сравнение и маршрутизация (не требуют БД)
# ---------------------------------------------------------------------------


def _record(title="Логин", description=None, fields=()):
    record = Record(
        title=title,
        description=description,
        record_type=RecordType.CREDENTIALS,
        status="ACTIVE",
    )
    record.fields = [
        RecordField(
            field_key=key,
            field_label=label,
            field_type=field_type,
            value_text=value_text,
            encrypted_value=encrypted,
            sort_order=index,
        )
        for index, (key, label, field_type, value_text, encrypted) in enumerate(fields)
    ]
    return record


def test_build_snapshot_keeps_secrets_encrypted():
    cipher = encrypt_secret("super-secret")
    record = _record(
        fields=[
            ("login", "Логин", "TEXT", "admin", None),
            ("password", "Пароль", "PASSWORD", None, cipher),
        ]
    )

    snapshot = build_snapshot(record)

    assert "super-secret" not in json.dumps(snapshot, ensure_ascii=False)
    assert snapshot["record_type"] == "CREDENTIALS"
    password = next(item for item in snapshot["fields"] if item["field_key"] == "password")
    assert password["encrypted_value"] == cipher
    assert decrypt_secret(password["encrypted_value"]) == "super-secret"


def test_compare_snapshots_reports_title_and_field_changes():
    old = build_snapshot(_record(title="A", fields=[("login", "Логин", "TEXT", "x", None)]))
    new = build_snapshot(
        _record(
            title="B",
            fields=[
                ("login", "Логин", "TEXT", "y", None),
                ("url", "Ссылка", "URL", "https://example.com", None),
            ],
        )
    )

    changes = {change.name: change for change in compare_snapshots(old, new)}

    assert changes["title"].old == "A"
    assert changes["title"].new == "B"
    assert changes["field:login"].old == "x"
    assert changes["field:login"].new == "y"
    assert changes["field:url"].old is None
    assert changes["field:url"].new == "https://example.com"
    assert "description" not in changes


def test_compare_snapshots_hides_secret_values():
    old = build_snapshot(
        _record(fields=[("password", "Пароль", "PASSWORD", None, encrypt_secret("first"))])
    )
    new = build_snapshot(
        _record(fields=[("password", "Пароль", "PASSWORD", None, encrypt_secret("second"))])
    )

    changes = compare_snapshots(old, new)

    assert len(changes) == 1
    assert changes[0].secret is True
    assert changes[0].old is None
    assert changes[0].new is None
    assert "first" not in repr(changes)
    assert "second" not in repr(changes)


def test_compare_snapshots_identical_versions_have_no_changes():
    snapshot = build_snapshot(
        _record(
            fields=[
                ("login", "Логин", "TEXT", "x", None),
                ("password", "Пароль", "PASSWORD", None, encrypt_secret("same")),
            ]
        )
    )

    assert compare_snapshots(snapshot, snapshot) == []


def test_version_errors_are_value_errors():
    for error in (RecordNotFound, VersionNotFound, VersionExists, VersionConflict):
        assert issubclass(error, RecordVersionError)
        assert issubclass(error, ValueError)


def test_records_router_exposes_history_and_restore_handlers():
    handler_names = {
        handler.callback.__name__ for handler in records_handlers.router.callback_query.handlers
    }
    assert {
        "show_history",
        "show_version",
        "restore_version_callback",
        "manage_tags",
        "pick_tag",
        "attach_tag",
        "detach_tag",
        "start_new_tag",
        "start_edit_record",
    } <= handler_names


def test_version_callbacks_fit_telegram_limit():
    record_id = str(uuid4())
    for data in (
        f"record:history:{record_id}",
        f"record:version:{record_id}:99999",
        f"record:restore:{record_id}:99999",
    ):
        assert len(data.encode()) <= TELEGRAM_CALLBACK_LIMIT


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
                await session.execute(select(Record.id).limit(1))
            except SQLAlchemyError:
                pytest.skip("Apply migrations to the test database first")
            yield session
    finally:
        await engine.dispose()


async def _make_record(session: AsyncSession, user: User, category_id, title: str) -> Record:
    record = Record(
        category_id=category_id,
        record_type=RecordType.CREDENTIALS,
        title=title,
        description="Описание",
        created_by=user.id,
        updated_by=user.id,
    )
    session.add(record)
    await session.commit()
    return record


async def _cleanup(session: AsyncSession, record_id, category_id) -> None:
    if record_id is not None:
        record = await session.get(Record, record_id)
        if record is not None:
            await session.delete(record)
            await session.commit()
    if category_id is not None:
        await session.execute(delete(Category).where(Category.id == category_id))
        await session.commit()


async def test_version_snapshots_lock_restore_and_diff(db_session):
    user = (await db_session.execute(select(User).limit(1))).scalar_one_or_none()
    if user is None:
        pytest.skip("The test database has no users")

    category = await create_category(
        db_session, f"ver_{uuid4().hex[:8]}", "Тест версий", category_type="root"
    )
    record = await _make_record(db_session, user, category.id, "Исходное")
    record_id = record.id
    try:
        v1 = await create_version(record_id, user.id, "Создание записи", db_session)
        assert v1.version == 1
        with pytest.raises(VersionExists):
            await create_version(record_id, user.id, "Дубликат", db_session)

        updated = await update_record_with_lock(
            record_id,
            1,
            db_session,
            changed_by=user.id,
            change_description="Правка",
            title="Новое",
        )
        assert updated.version == 2
        assert updated.title == "Новое"

        with pytest.raises(VersionConflict):
            await update_record_with_lock(
                record_id,
                1,
                db_session,
                changed_by=user.id,
                change_description="Устаревшая правка",
                title="Конфликт",
            )

        versions = await list_versions(record_id, db_session)
        assert [version.version for version in versions] == [2, 1]

        changes = await get_version_diff(record_id, 1, 2, db_session)
        title_change = next(change for change in changes if change.name == "title")
        assert (title_change.old, title_change.new) == ("Исходное", "Новое")

        restored = await restore_version(record_id, 1, db_session, changed_by=user.id)
        assert restored.title == "Исходное"
        assert restored.version == 3

        versions = await list_versions(record_id, db_session)
        assert [version.version for version in versions] == [3, 2, 1]
        assert versions[0].change_description == "Восстановлено из версии 1"

        with pytest.raises(VersionNotFound):
            await restore_version(record_id, 99, db_session, changed_by=user.id)
        with pytest.raises(VersionNotFound):
            await get_version_diff(record_id, 1, 99, db_session)
    finally:
        await _cleanup(db_session, record_id, category.id)


async def test_restore_brings_back_secret_fields_without_plaintext_changes(db_session):
    user = (await db_session.execute(select(User).limit(1))).scalar_one_or_none()
    if user is None:
        pytest.skip("The test database has no users")

    category = await create_category(
        db_session, f"ver_{uuid4().hex[:8]}", "Тест секретов", category_type="root"
    )
    record = await _make_record(db_session, user, category.id, "С секретом")
    record_id = record.id
    try:
        db_session.add(
            RecordField(
                record_id=record_id,
                field_key="password",
                field_label="Пароль",
                field_type="PASSWORD",
                encrypted_value=encrypt_secret("old-pass"),
                sort_order=0,
            )
        )
        await db_session.commit()
        await create_version(record_id, user.id, "Создание записи", db_session)

        await db_session.execute(delete(RecordField).where(RecordField.record_id == record_id))
        await db_session.commit()
        await update_record_with_lock(
            record_id,
            1,
            db_session,
            changed_by=user.id,
            change_description="Удаление поля",
            title="Без секрета",
        )

        restored = await restore_version(record_id, 1, db_session, changed_by=user.id)

        assert restored.title == "С секретом"
        fields = (
            (await db_session.execute(select(RecordField).where(RecordField.record_id == record_id)))
            .scalars()
            .all()
        )
        assert [field.field_key for field in fields] == ["password"]
        assert decrypt_secret(fields[0].encrypted_value) == "old-pass"
    finally:
        await _cleanup(db_session, record_id, category.id)
