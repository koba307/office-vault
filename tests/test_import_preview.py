"""Тесты предпросмотра импорта и вспомогательной логики задач (Phase 08)."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.bot.handlers.import_export as import_handler
from app.database.models import JobStatus, RecordType, User
from app.services.import_export.preview import (
    ImportPreview,
    PreviewError,
    PreviewFormatError,
    RowError,
    ValidationResult,
    apply_field_mapping,
    preview_import,
    suggest_field_mapping,
    validate_rows,
)
from app.services.import_export.service import (
    ImportCancelled,
    ImportJobError,
    ImportJobNotFound,
    ImportResult,
    ImportStatus,
    _secret_field_keys,
    _split_row,
    cancel_import,
    create_import_job,
    get_import_status,
    import_records,
)

# ---------------------------------------------------------------------------
# Сопоставление полей
# ---------------------------------------------------------------------------


def test_suggest_field_mapping_russian_headers():
    mapping = suggest_field_mapping(
        ["Название", "Тип", "Описание", "Логин", "Пароль"], RecordType.CREDENTIALS
    )
    assert mapping == {
        "Название": "title",
        "Тип": "type",
        "Описание": "description",
        "Логин": "login",
        "Пароль": "password",
    }


def test_suggest_field_mapping_english_and_partial_matches():
    mapping = suggest_field_mapping(["Title", "Login сотрудника", "Ссылка на сайт"])
    assert mapping["Title"] == "title"
    assert mapping["Login сотрудника"] == "login"
    assert mapping["Ссылка на сайт"] == "url"


def test_suggest_field_mapping_generates_dynamic_key():
    mapping = suggest_field_mapping(["CRM ID", "Штрихкод"])
    assert mapping["CRM ID"] == "crm_id"
    assert "Штрихкод" not in mapping


def test_suggest_field_mapping_does_not_reuse_target():
    mapping = suggest_field_mapping(["Название", "Заголовок"])
    assert mapping["Название"] == "title"
    assert "Заголовок" not in mapping


def test_apply_field_mapping_uses_column_to_field_direction():
    rows = [{"Колонка": "Альфа", "Логин": "user1", "Пусто": ""}]
    mapping = {"Колонка": "title", "Логин": "login", "Пусто": "notes"}
    assert apply_field_mapping(rows, mapping) == [{"title": "Альфа", "login": "user1"}]


def test_apply_field_mapping_merges_duplicate_targets():
    rows = [{"A": "one", "B": "two"}]
    assert apply_field_mapping(rows, {"A": "notes", "B": "notes"}) == [{"notes": "one; two"}]


def test_apply_field_mapping_without_mapping_keeps_rows():
    rows = [{"title": "A"}]
    assert apply_field_mapping(rows, {}) == [{"title": "A"}]


# ---------------------------------------------------------------------------
# Валидация строк
# ---------------------------------------------------------------------------


def test_validate_rows_requires_title():
    result = validate_rows([{"title": ""}, {"title": "A"}])
    assert isinstance(result, ValidationResult)
    assert result.total_rows == 2
    assert result.valid_rows == 1
    assert result.invalid_rows == 1
    assert result.errors[0].row == 1
    assert result.errors[0].field == "title"


def test_validate_rows_checks_required_fields_of_record_type():
    rows = [{"title": "A", "login": "user"}, {"title": "B", "login": "u", "password": "p"}]
    result = validate_rows(rows, RecordType.CREDENTIALS)
    assert result.invalid_rows == 1
    assert result.errors[0].row == 1
    assert result.errors[0].field == "password"
    assert result.valid_rows == 1


def test_validate_rows_detects_duplicates_in_file():
    result = validate_rows([{"title": "Alpha"}, {"title": "alpha"}])
    assert result.invalid_rows == 1
    assert result.duplicates == 1
    assert any("Дубликат" in error.message for error in result.errors)


def test_validate_rows_detects_existing_titles():
    result = validate_rows([{"title": "Alpha"}], known_titles={"ALPHA"})
    assert result.invalid_rows == 1
    assert result.duplicates == 1
    assert "уже есть" in result.errors[0].message


def test_validate_rows_rejects_unknown_type():
    result = validate_rows([{"title": "A", "type": "UNKNOWN"}])
    assert result.invalid_rows == 1
    assert result.errors[0].field == "type"


def test_row_error_serializes():
    assert RowError(1, "title", "bad").as_dict() == {
        "row": 1,
        "field": "title",
        "message": "bad",
    }


# ---------------------------------------------------------------------------
# Предпросмотр
# ---------------------------------------------------------------------------


async def test_preview_import_csv_without_database():
    content = "Название,Тип,Логин,Пароль\nAlpha,CREDENTIALS,user1,secret\nBeta,OTHER,user2,\n"
    preview = await preview_import(
        None, content.encode(), "records.csv", "general", record_type=RecordType.CREDENTIALS
    )
    assert isinstance(preview, ImportPreview)
    assert preview.total_rows == 2
    assert preview.valid_rows == 1
    assert preview.invalid_rows == 1
    assert preview.source_format == "CSV"
    assert preview.record_type == "CREDENTIALS"
    assert preview.suggested_mapping["Название"] == "title"
    assert preview.suggested_mapping["Пароль"] == "password"
    assert preview.sample_rows[0]["login"] == "user1"
    assert preview.duplicate_rows == 0


async def test_preview_import_custom_mapping_changes_result():
    content = "col_a;col_b\nAlpha;user1\n"
    preview = await preview_import(
        None,
        content.encode(),
        "records.csv",
        "general",
        mapping={"col_a": "title", "col_b": "login"},
    )
    assert preview.valid_rows == 1
    assert preview.suggested_mapping == {"col_a": "title", "col_b": "login"}
    assert preview.sample_rows == [{"title": "Alpha", "login": "user1"}]


async def test_preview_import_json_and_txt():
    json_content = b'[{"title": "A", "login": "u1"}, {"title": "B", "login": "u2"}]'
    json_preview = await preview_import(None, json_content, "records.json", "general")
    assert json_preview.source_format == "JSON"
    assert json_preview.valid_rows == 2

    txt_preview = await preview_import(None, b"first\nsecond\n", "notes.txt", "general")
    assert txt_preview.suggested_mapping == {"text": "title"}
    assert txt_preview.total_rows == 2
    assert txt_preview.valid_rows == 2


async def test_preview_import_reports_duplicates_and_truncates_errors():
    content = "title\n" + "\n".join(["same"] * 3)
    preview = await preview_import(None, content.encode(), "records.csv", "general")
    assert preview.total_rows == 3
    assert preview.invalid_rows == 2
    assert preview.duplicate_rows == 2


async def test_preview_import_rejects_empty_and_unsupported_files():
    with pytest.raises(PreviewFormatError):
        await preview_import(None, b"", "empty.csv", "general")
    with pytest.raises(PreviewFormatError):
        await preview_import(None, b"   ", "file.unknown", "general")


async def test_preview_import_validates_record_type():
    with pytest.raises(PreviewError):
        await preview_import(None, b"title\nA\n", "records.csv", "general", record_type="NOPE")


# ---------------------------------------------------------------------------
# Разбор строк для фонового импорта
# ---------------------------------------------------------------------------


def test_split_row_extracts_reserved_and_dynamic_fields():
    title, record_type, description, fields, tags = _split_row(
        {
            "title": " Alpha ",
            "type": "CREDENTIALS",
            "description": " note ",
            "tags": "one, two;three",
            "login": "user",
            "password": "",
        }
    )
    assert title == "Alpha"
    assert record_type is RecordType.CREDENTIALS
    assert description == "note"
    assert fields == {"login": "user"}
    assert tags == ["one", "two", "three"]


def test_split_row_falls_back_to_other_type():
    _, record_type, _, _, _ = _split_row({"title": "A", "type": "bad"})
    assert record_type is RecordType.OTHER


def test_secret_field_keys_detects_password_and_token():
    assert _secret_field_keys({"password": "x", "api_key": "y", "login": "z"}) == {
        "password",
        "api_key",
    }
    assert _secret_field_keys({}) == set()


def test_import_result_and_status_helpers():
    result = ImportResult(job_id=None, status=JobStatus.COMPLETED, total_rows=2, success_rows=1)
    assert result.failed_rows == 0
    status = ImportStatus(
        job_id=uuid4(),
        status=JobStatus.PROCESSING,
        total_rows=10,
        success_rows=4,
        failed_rows=1,
    )
    assert status.processed_rows == 5
    assert status.percent == 50


def test_import_job_errors_are_value_errors():
    assert issubclass(ImportJobError, ValueError)
    assert issubclass(ImportJobNotFound, ImportJobError)
    assert issubclass(ImportCancelled, ImportJobError)


# ---------------------------------------------------------------------------
# FSM импорта: сериализация состояния и клавиатуры
# ---------------------------------------------------------------------------


def test_parse_mapping_message_uses_known_headers():
    mapping = import_handler._parse_mapping_message(
        "название = Title\nЛогин=Login\nмусор", ["Название", "Логин"]
    )
    assert mapping == {"Название": "title", "Логин": "login"}


def test_preview_payload_roundtrip_for_fsm_storage():
    preview = ImportPreview(
        total_rows=3,
        valid_rows=2,
        invalid_rows=1,
        errors=[RowError(2, "title", "нет названия")],
        suggested_mapping={"A": "title"},
        headers=["A"],
        source_format="CSV",
        encoding="utf-8",
    )
    payload = import_handler._preview_payload(preview)
    restored = import_handler._preview_from_state({"preview": payload})
    assert restored is not None
    assert restored.total_rows == 3
    assert restored.errors == [RowError(2, "title", "нет названия")]
    assert restored.suggested_mapping == {"A": "title"}


def test_import_keyboards_fit_callback_limit():
    keyboards = [
        import_handler._category_keyboard([("general", "Общее")]),
        import_handler._file_keyboard(),
        import_handler._preview_keyboard(),
        import_handler._mapping_keyboard(),
        import_handler._confirm_keyboard(),
        import_handler._report_keyboard(),
    ]
    for keyboard in keyboards:
        for row in keyboard.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= 64


# ---------------------------------------------------------------------------
# Интеграционные тесты задач (нужен TEST_DATABASE_URL)
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
                await session.execute(select(User.id).limit(1))
            except SQLAlchemyError:
                pytest.skip("Apply migrations to the test database first")
            yield session
    finally:
        await engine.dispose()


async def test_import_job_lifecycle(db_session):
    user = (await db_session.execute(select(User).limit(1))).scalar_one_or_none()
    if user is None:
        pytest.skip("The test database has no users")

    from app.services.categories import create_category

    category = await create_category(
        db_session, f"import_{uuid4().hex[:8]}", "Тест импорта", category_type="root"
    )
    category_ids = [category.id]
    job = await create_import_job(
        db_session, user, category.code, source_format="CSV", total_rows=2
    )
    first_job_id = job.id
    status = await get_import_status(db_session, job.id)
    assert status is not None
    assert status.status.value == "PENDING"
    assert status.total_rows == 2
    assert await cancel_import(db_session, job.id) is True
    assert await cancel_import(db_session, job.id) is False

    assert await get_import_status(db_session, uuid4()) is None

    rows = [
        {"title": f"Импорт {uuid4().hex[:6]}", "description": "x", "login": "u1"},
        {"title": f"Импорт {uuid4().hex[:6]}", "type": "CREDENTIALS", "login": "u2"},
        {"title": f"Импорт {uuid4().hex[:6]}", "description": "y", "login": "u3"},
    ]
    job = await create_import_job(db_session, user, category.code, source_format="CSV")
    job_id = job.id
    result = await import_records(db_session, job_id, {}, rows=rows)
    # Строка без обязательного пароля падает, но не останавливает импорт.
    assert result.success_rows == 2
    assert result.failed_rows == 1
    status = await get_import_status(db_session, job_id)
    assert status is not None
    assert status.status.value == "COMPLETED"
    assert status.success_rows == 2
    assert status.failed_rows == 1

    with pytest.raises(ImportJobError):
        await import_records(db_session, job_id, {}, rows=rows)

    from sqlalchemy import delete

    from app.database.models import Category, ImportError, ImportJob, Record, RecordField, RecentRecord

    await db_session.execute(delete(ImportError).where(ImportError.job_id.in_([first_job_id, job_id])))
    await db_session.execute(delete(ImportJob).where(ImportJob.id.in_([first_job_id, job_id])))
    await db_session.execute(
        delete(RecentRecord).where(RecentRecord.record_id.in_(select(Record.id).where(Record.category_id.in_(category_ids))))
    )
    await db_session.execute(
        delete(RecordField).where(RecordField.record_id.in_(select(Record.id).where(Record.category_id.in_(category_ids))))
    )
    await db_session.execute(delete(Record).where(Record.category_id.in_(category_ids)))
    await db_session.execute(delete(Category).where(Category.id.in_(category_ids)))
    await db_session.commit()
