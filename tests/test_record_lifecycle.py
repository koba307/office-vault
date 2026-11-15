"""Тесты жизненного цикла записей Phase 05.

Перемещение, восстановление из архива, окончательное удаление и вложения.
Чистые тесты не требуют базы; интеграционные используют TEST_DATABASE_URL
(изолированная PostgreSQL с применёнными миграциями).
"""

import inspect
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.handlers import archive as archive_handlers
from app.bot.handlers import records as records_handlers
from app.bot.handlers.archive import (
    ALL_FILTER,
    CALLBACK_LIMIT,
    build_archive_keyboard,
    parse_archive_callback,
)
from app.bot.handlers.records import (
    MAX_ATTACHMENT_BYTES,
    _attachments_view,
    _human_size,
    lifecycle_buttons,
)
from app.database.models import (
    Category,
    Favorite,
    File,
    Record,
    RecordField,
    RecordFile,
    RecordType,
)
from app.services.records import service as records_service
from app.services.records.service import (
    PERMANENTLY_DELETABLE_STATUSES,
    RESTORABLE_STATUSES,
    RecordError,
    RecordNotFound,
    RecordPermissionError,
    RecordStateError,
    RecordValidationError,
    add_attachment,
    list_attachments,
    move_record,
    permanent_delete,
    remove_attachment,
    restore_record,
)

# ---------------------------------------------------------------------------
# Чистые тесты: API сервиса (не требуют БД)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "move_record",
        "restore_record",
        "permanent_delete",
        "add_attachment",
        "remove_attachment",
        "list_attachments",
    ],
)
def test_lifecycle_service_api_exists(name):
    function = getattr(records_service, name)
    assert inspect.iscoroutinefunction(function)
    # Соглашение проекта: session — первый positional-аргумент.
    parameters = list(inspect.signature(function).parameters)
    assert parameters[0] == "session"


@pytest.mark.parametrize("name", ["move_record", "restore_record", "permanent_delete"])
def test_lifecycle_functions_accept_optional_user_for_permission_check(name):
    function = getattr(records_service, name)
    parameters = inspect.signature(function).parameters
    user = parameters["user"]
    assert user.kind is inspect.Parameter.KEYWORD_ONLY
    assert user.default is None

def test_lifecycle_statuses_cover_hidden_records():
    assert PERMANENTLY_DELETABLE_STATUSES == frozenset({"ARCHIVED", "DELETED"})
    assert RESTORABLE_STATUSES == frozenset({"ARCHIVED", "DELETED"})


def test_lifecycle_errors_share_record_error_hierarchy():
    for error in (RecordNotFound, RecordPermissionError, RecordStateError, RecordValidationError):
        assert issubclass(error, RecordError)
        assert issubclass(error, ValueError)


# ---------------------------------------------------------------------------
# Кнопки карточки: Переместить / Вложения / Восстановить / Удалить окончательно
# ---------------------------------------------------------------------------


class _StubRecord:
    def __init__(self, status="ACTIVE"):
        self.id = uuid4()
        self.status = status
        self.title = "Тестовая запись"


def _collect_callbacks(rows):
    return [btn.callback_data for row in rows for btn in row]


def _row_texts(rows):
    return [btn.text for row in rows for btn in row]


def test_lifecycle_buttons_for_active_record():
    record = _StubRecord("ACTIVE")
    rows = lifecycle_buttons(record, can_edit=True, can_archive=True, can_delete=True)
    texts = _row_texts(rows)
    assert any("Переместить" in text for text in texts)
    assert any("Вложения" in text for text in texts)
    assert any("Архивировать" in text for text in texts)
    assert any("Удалить" in text and "окончательно" not in text for text in texts)
    assert not any("Восстановить" in text for text in texts)


def test_lifecycle_buttons_for_hidden_record_offer_restore_and_purge():
    for status in ("ARCHIVED", "DELETED"):
        record = _StubRecord(status)
        rows = lifecycle_buttons(record, can_edit=True, can_archive=True, can_delete=True)
        texts = _row_texts(rows)
        assert any("Восстановить" in text for text in texts)
        assert any("Удалить окончательно" in text for text in texts)
        assert not any("Архивировать" in text for text in texts)
        callbacks = _collect_callbacks(rows)
        assert any(c.startswith("record:unarchive:") for c in callbacks)
        assert any(c.startswith("record:pdelete:") for c in callbacks)


def test_lifecycle_buttons_hidden_without_permissions():
    record = _StubRecord("ARCHIVED")
    rows = lifecycle_buttons(record, can_edit=True, can_archive=False, can_delete=False)
    texts = _row_texts(rows)
    assert not any("Восстановить" in text for text in texts)
    assert not any("окончательно" in text for text in texts)


def test_lifecycle_buttons_hidden_for_non_manager():
    assert lifecycle_buttons(_StubRecord("ARCHIVED"), can_edit=False) == []


def test_lifecycle_callbacks_fit_telegram_limit():
    record = _StubRecord("ACTIVE")
    rows = lifecycle_buttons(record, can_edit=True, can_archive=True, can_delete=True)
    for callback in _collect_callbacks(rows):
        assert len(callback.encode()) <= CALLBACK_LIMIT
    # Двух UUID в одном адресе быть не может — перемещение помнит запись в FSM.
    for callback in _collect_callbacks(rows):
        assert callback.count(str(record.id)) <= 1


def test_purge_confirm_callback_fits_limit():
    rid = uuid4()
    confirm = f"record:pdelete_yes:{rid}"
    assert len(confirm.encode()) <= CALLBACK_LIMIT
    # Подтверждение не должно перехватываться фильтром запроса.
    assert not confirm.startswith("record:pdelete:")


def test_new_prefixes_are_not_shadowed_by_existing_filters():
    rid, fid = uuid4(), uuid4()
    mine = [
        f"record:move:{rid}",
        f"record:move_to:{rid}",
        f"record:unarchive:{rid}",
        f"record:pdelete:{rid}",
        f"record:pdelete_yes:{rid}",
        f"record:files:{rid}",
        f"record:file_down:{fid}",
        f"record:file_add:{rid}",
        f"record:file_del:{fid}",
    ]
    registered_prefixes = [
        "record:add:", "record:archive:", "record:delete:", "record:download:",
        "record:edit:", "record:edit_title:", "record:edit_desc:", "record:edit_field:",
        "record:edit_add:", "record:edit_tags:", "record:ftype:", "record:etype:",
        "record:reveal:", "record:view:", "rr:",
    ]
    for callback in mine:
        for prefix in registered_prefixes:
            assert not callback.startswith(prefix), f"{callback} перехвачен {prefix}"


# ---------------------------------------------------------------------------
# Экран вложений: список / добавить файл / удалить файл
# ---------------------------------------------------------------------------


class _StubFile:
    def __init__(self, name="Отчёт.pdf", size=2_500_000):
        self.id = uuid4()
        self.original_name = name
        self.size = size
        self.mime_type = "application/pdf"


def test_attachments_view_lists_files_with_actions():
    record = _StubRecord("ACTIVE")
    first, second = _StubFile("Акт.pdf"), _StubFile(" База.xlsx", 1024)
    text, markup = _attachments_view(record, [first, second], can_manage=True)
    assert "Акт.pdf" in text
    assert second.original_name.strip() in text
    callbacks = _collect_callbacks(markup.inline_keyboard)
    assert f"record:file_down:{first.id}" in callbacks
    assert f"record:file_down:{second.id}" in callbacks
    assert f"record:file_del:{first.id}" in callbacks
    assert f"record:file_del:{second.id}" in callbacks
    texts = _row_texts(markup.inline_keyboard)
    assert any("Добавить файл" in t for t in texts)
    assert sum("Удалить файл" in t for t in texts) == 2


def test_attachments_view_read_only_mode():
    record = _StubRecord("ACTIVE")
    file = _StubFile()
    _, markup = _attachments_view(record, [file], can_manage=False)
    texts = _row_texts(markup.inline_keyboard)
    assert not any("Добавить файл" in t for t in texts)
    assert not any("Удалить файл" in t for t in texts)
    # Скачивание остаётся доступным.
    assert f"record:file_down:{file.id}" in _collect_callbacks(markup.inline_keyboard)


def test_attachments_view_empty_state():
    record = _StubRecord("ACTIVE")
    text, markup = _attachments_view(record, [], can_manage=True)
    assert "Файлов пока нет" in text
    assert f"record:file_add:{record.id}" in _collect_callbacks(markup.inline_keyboard)


def test_attachment_callbacks_fit_telegram_limit():
    record, file = _StubRecord(), _StubFile()
    _, markup = _attachments_view(record, [file], can_manage=True)
    for callback in _collect_callbacks(markup.inline_keyboard):
        assert len(callback.encode()) <= CALLBACK_LIMIT


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0 Б"),
        (512, "512 Б"),
        (2048, "2.0 КБ"),
        (2_500_000, "2.4 МБ"),
        (5 * 1024 * 1024 * 1024, "5.0 ГБ"),
        (None, "0 Б"),
    ],
)
def test_human_size(value, expected):
    assert _human_size(value) == expected


def test_attachment_size_limit_is_below_telegram_cap():
    assert MAX_ATTACHMENT_BYTES <= 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Архив: разбор callback-адресов и клавиатура экрана
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data,expected",
    [
        ("archive:l:a:holodka:CREDENTIALS:0", ("a", "holodka", "CREDENTIALS", 0)),
        ("archive:l:d:general:ALL:3", ("d", "general", "ALL", 3)),
        ("archive:l:a:my_sub-1:INSTRUCTIONS:12", ("a", "my_sub-1", "INSTRUCTIONS", 12)),
        ("archive:l:a:ALL:ALL:0", ("a", "ALL", "ALL", 0)),
    ],
)
def test_parse_archive_callback_accepts_valid_data(data, expected):
    assert parse_archive_callback(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        None,
        "",
        "archive:l:x:holodka:LINKS:0",
        "archive:l:a:Holodka:LINKS:0",
        "archive:l:a:bad code:LINKS:0",
        "archive:l:a:holodka:UNKNOWN:0",
        "archive:l:a:holodka:LINKS:-1",
        "archive:l:a:holodka:LINKS:abc",
        "archive:l:a:holodka:LINKS",
        "archive:list:a:holodka:LINKS:0",
        "menu:archive",
    ],
)
def test_parse_archive_callback_rejects_stale_data(data):
    assert parse_archive_callback(data) is None


def _stub_archive_row(status="ARCHIVED"):
    record = _StubRecord(status)
    record.title = "Тестовая запись"
    return (record, "general")


def test_build_archive_keyboard_actions_follow_permissions():
    rows_data = [_stub_archive_row()]
    markup = build_archive_keyboard(
        rows_data,
        total=1,
        page=0,
        status="a",
        category=ALL_FILTER,
        record_type=ALL_FILTER,
        can_restore=True,
        can_purge=True,
    )
    callbacks = _collect_callbacks(markup.inline_keyboard)
    assert any(c.startswith("archive:restore:") for c in callbacks)
    assert any(c.startswith("archive:purge:") for c in callbacks)
    assert any(c.startswith("record:view:") for c in callbacks)
    assert "menu:main" in callbacks

    markup = build_archive_keyboard(
        rows_data,
        total=1,
        page=0,
        status="a",
        category=ALL_FILTER,
        record_type=ALL_FILTER,
        can_restore=False,
        can_purge=False,
    )
    callbacks = _collect_callbacks(markup.inline_keyboard)
    assert not any(c.startswith("archive:restore:") for c in callbacks)
    assert not any(c.startswith("archive:purge:") for c in callbacks)


def test_build_archive_keyboard_callbacks_fit_telegram_limit():
    long_code = "a" * 32
    rows_data = [_stub_archive_row() for _ in range(5)]
    markup = build_archive_keyboard(
        rows_data,
        total=99,
        page=2,
        status="d",
        category=long_code,
        record_type="INSTRUCTIONS",
        can_restore=True,
        can_purge=True,
    )
    for callback in _collect_callbacks(markup.inline_keyboard):
        assert len(callback.encode()) <= CALLBACK_LIMIT
    # Все разобранные адреса списка должны возвращаться парсером.
    for callback in _collect_callbacks(markup.inline_keyboard):
        if callback.startswith("archive:l:"):
            assert parse_archive_callback(callback) is not None


def test_archive_purge_confirmation_roundtrip_callbacks():
    rid = uuid4()
    ask = f"archive:purge:{rid}"
    yes = f"archive:purge_yes:{rid}"
    assert len(ask.encode()) <= CALLBACK_LIMIT
    assert len(yes.encode()) <= CALLBACK_LIMIT
    assert not yes.startswith("archive:purge:")


# ---------------------------------------------------------------------------
# Регистрация роутеров
# ---------------------------------------------------------------------------

_RECORDS_CALLBACK_HANDLERS = {
    "move_flow",
    "do_restore",
    "ask_permanent_delete",
    "do_permanent_delete",
    "attachments_screen",
    "download_attachment",
    "start_attachment_upload",
    "remove_attachment_link",
}

_ARCHIVE_CALLBACK_HANDLERS = {
    "archive_from_menu",
    "archive_open",
    "archive_list",
    "archive_filter_category",
    "archive_filter_type",
    "archive_restore",
    "archive_purge_ask",
    "archive_purge_confirm",
}


def test_records_router_exposes_lifecycle_handlers():
    names = {
        handler.callback.__name__
        for handler in records_handlers.router.callback_query.handlers
    }
    assert _RECORDS_CALLBACK_HANDLERS <= names


def test_records_router_exposes_attachment_upload_message_handlers():
    names = {
        handler.callback.__name__ for handler in records_handlers.router.message.handlers
    }
    assert {"receive_attachment", "attachment_wrong_input"} <= names


def test_archive_router_exposes_all_handlers():
    names = {
        handler.callback.__name__ for handler in archive_handlers.router.callback_query.handlers
    }
    assert _ARCHIVE_CALLBACK_HANDLERS <= names


def test_archive_router_is_registered_before_menu_router():
    """Полный экран архива Phase 05 перекрывает упрощённый menu:archive из Phase 04."""
    source = Path("app/bot/main.py").read_text(encoding="utf-8")
    archive_pos = source.index("dp.include_router(archive.router)")
    menu_pos = source.index("dp.include_router(menu.router)")
    assert archive_pos < menu_pos


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


async def _first_user(session):
    from app.database.models import User

    return (await session.execute(select(User).limit(1))).scalar_one_or_none()


async def _drop_records(session, record_ids):
    """Удаляет записи через core: поля без каскада в БД, ORM в async не надёжен."""
    for record_id in record_ids:
        await session.execute(delete(RecordField).where(RecordField.record_id == record_id))
        await session.execute(delete(RecordFile).where(RecordFile.record_id == record_id))
        await session.execute(delete(Favorite).where(Favorite.record_id == record_id))
        await session.execute(delete(Record).where(Record.id == record_id))
    await session.commit()


async def _drop_categories(session, category_ids):
    if category_ids:
        await session.execute(delete(Category).where(Category.id.in_(list(category_ids))))
    await session.commit()


async def _drop_files(session, file_ids):
    if file_ids:
        await session.execute(delete(File).where(File.id.in_(list(file_ids))))
    await session.commit()


async def _make_category(session, name, *, status="ACTIVE", parent_id=None):
    code = f"lc_{uuid4().hex[:10]}"
    category = Category(
        code=code,
        name=name,
        parent_id=parent_id,
        category_type="root" if parent_id is None else "subcategory",
        status=status,
        sort_order=99,
    )
    session.add(category)
    await session.commit()
    await session.refresh(category)
    return category


async def _make_record(session, user, category, *, status="ACTIVE"):
    record = Record(
        category_id=category.id,
        record_type=RecordType.OTHER,
        title="Жизненный цикл",
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


async def _make_file(session, user):
    file = File(
        original_name="тестовое вложение.bin",
        storage_key=f"tests/lifecycle/{uuid4().hex}",
        bucket="office-vault-test",
        mime_type="application/octet-stream",
        extension="bin",
        size=123,
        uploaded_by=user.id,
    )
    session.add(file)
    await session.commit()
    await session.refresh(file)
    return file


async def test_move_record_changes_category_and_resets_subcategory(db_session):
    user = await _first_user(db_session)
    if user is None:
        pytest.skip("The test database has no users")
    source = await _make_category(db_session, "Источник")
    root = await _make_category(db_session, "Целевой раздел")
    sub = await _make_category(db_session, "Подкатегория", parent_id=root.id)
    record = await _make_record(db_session, user, source)
    record.subcategory_id = sub.id
    await db_session.commit()

    try:
        moved = await move_record(db_session, record.id, root.id, user.id)

        assert moved.category_id == root.id
        assert moved.subcategory_id is None
        assert moved.updated_by == user.id
    finally:
        await _drop_records(db_session, [record.id])
        await _drop_categories(db_session, [sub.id, root.id, source.id])


async def test_move_record_rejects_unknown_or_inactive_category(db_session):
    user = await _first_user(db_session)
    if user is None:
        pytest.skip("The test database has no users")
    source = await _make_category(db_session, "Источник")
    inactive = await _make_category(db_session, "Отключённый", status="DISABLED")
    record = await _make_record(db_session, user, source)

    try:
        with pytest.raises(RecordValidationError):
            await move_record(db_session, record.id, uuid4(), user.id)
        with pytest.raises(RecordValidationError):
            await move_record(db_session, record.id, inactive.id, user.id)
    finally:
        await _drop_records(db_session, [record.id])
        await _drop_categories(db_session, [inactive.id, source.id])


async def test_move_record_rejects_missing_and_deleted_records(db_session):
    user = await _first_user(db_session)
    if user is None:
        pytest.skip("The test database has no users")
    source = await _make_category(db_session, "Источник")
    target = await _make_category(db_session, "Цель")
    deleted = await _make_record(db_session, user, source, status="DELETED")

    try:
        with pytest.raises(RecordNotFound):
            await move_record(db_session, uuid4(), target.id, user.id)
        with pytest.raises(RecordStateError):
            await move_record(db_session, deleted.id, target.id, user.id)
    finally:
        await _drop_records(db_session, [deleted.id])
        await _drop_categories(db_session, [target.id, source.id])


async def test_restore_record_from_archive_and_trash(db_session):
    user = await _first_user(db_session)
    if user is None:
        pytest.skip("The test database has no users")
    category = await _make_category(db_session, "Раздел")
    record_ids = []

    try:
        for status in ("ARCHIVED", "DELETED"):
            record = await _make_record(db_session, user, category, status=status)
            record_ids.append(record.id)
            restored = await restore_record(db_session, record.id, user.id)
            assert restored is not None
            assert restored.status == "ACTIVE"

            # Повторное восстановление активной записи — no-op.
            assert await restore_record(db_session, record.id, user.id) is None

        assert await restore_record(db_session, uuid4(), user.id) is None
    finally:
        await _drop_records(db_session, record_ids)
        await _drop_categories(db_session, [category.id])


async def test_permanent_delete_blocks_active_records(db_session):
    user = await _first_user(db_session)
    if user is None:
        pytest.skip("The test database has no users")
    category = await _make_category(db_session, "Раздел")
    record = await _make_record(db_session, user, category)

    try:
        with pytest.raises(RecordStateError):
            await permanent_delete(db_session, record.id, user.id)
        with pytest.raises(RecordNotFound):
            await permanent_delete(db_session, uuid4(), user.id)
        # Запись осталась на месте.
        assert await db_session.get(Record, record.id) is not None
    finally:
        await _drop_records(db_session, [record.id])
        await _drop_categories(db_session, [category.id])


async def test_permanent_delete_removes_record_children_and_favorite(db_session):
    user = await _first_user(db_session)
    if user is None:
        pytest.skip("The test database has no users")
    category = await _make_category(db_session, "Раздел")
    record = await _make_record(db_session, user, category, status="ARCHIVED")
    db_session.add(Favorite(user_id=user.id, record_id=record.id))
    await db_session.commit()

    try:
        await permanent_delete(db_session, record.id, user.id)

        assert await db_session.get(Record, record.id) is None
        fields = (
            await db_session.execute(select(RecordField).where(RecordField.record_id == record.id))
        ).scalars().all()
        assert list(fields) == []
        favorites = (
            await db_session.execute(select(Favorite).where(Favorite.record_id == record.id))
        ).scalars().all()
        assert list(favorites) == []
    finally:
        await _drop_records(db_session, [record.id])
        await _drop_categories(db_session, [category.id])


async def test_attachment_roundtrip(db_session):
    user = await _first_user(db_session)
    if user is None:
        pytest.skip("The test database has no users")
    category = await _make_category(db_session, "Раздел")
    record = await _make_record(db_session, user, category)
    file = await _make_file(db_session, user)

    try:
        link = await add_attachment(db_session, record.id, file.id)
        assert link.record_id == record.id
        assert link.file_id == file.id
        # Повторная привязка идемпотентна.
        again = await add_attachment(db_session, record.id, file.id)
        assert again.sort_order == link.sort_order

        attachments = await list_attachments(db_session, record.id)
        assert [item.id for item in attachments] == [file.id]

        removed = await remove_attachment(db_session, record.id, file.id)
        assert removed is True
        assert await list_attachments(db_session, record.id) == []
        assert await remove_attachment(db_session, record.id, file.id) is False

        with pytest.raises(RecordValidationError):
            await add_attachment(db_session, record.id, uuid4())
        with pytest.raises(RecordNotFound):
            await add_attachment(db_session, uuid4(), file.id)
    finally:
        await _drop_records(db_session, [record.id])
        await _drop_files(db_session, [file.id])
        await _drop_categories(db_session, [category.id])


async def test_permanent_delete_keeps_file_but_removes_link(db_session):
    user = await _first_user(db_session)
    if user is None:
        pytest.skip("The test database has no users")
    category = await _make_category(db_session, "Раздел")
    record = await _make_record(db_session, user, category, status="DELETED")
    file = await _make_file(db_session, user)
    await add_attachment(db_session, record.id, file.id)

    try:
        await permanent_delete(db_session, record.id, user.id)

        assert await db_session.get(File, file.id) is not None
        assert (
            await db_session.get(RecordFile, {"record_id": record.id, "file_id": file.id}) is None
        )
    finally:
        await _drop_files(db_session, [file.id])
        await _drop_categories(db_session, [category.id])
