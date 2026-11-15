import os
from datetime import date, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.handlers import records
from app.bot.states.add_record import AddRecord, EditRecord
from app.core.encryption import decrypt_secret
from app.database.models import FieldType, RecordType
from app.services.records import fields
from app.services.records.fields import (
    FIELD_TYPE_HINTS,
    FIELD_TYPE_LABELS,
    SECRET_MASK,
    EncodedField,
    FieldValidationError,
    MissingRequiredField,
    default_field_definitions,
    encode_field_value,
    format_field_value,
    format_raw_field_value,
    normalize_field_key,
    parse_multiselect,
    required_field_definitions,
    validate_field_value,
    validate_required_fields,
)
from app.services.records.service import (
    RecordValidationError,
    normalize_access_mode,
    validate_record_description,
    validate_record_title,
)

TELEGRAM_CALLBACK_LIMIT = 64
SAMPLE_UUID = "00000000-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# Типы полей и шаблоны записей
# ---------------------------------------------------------------------------


def test_every_field_type_has_label_and_hint():
    assert set(FIELD_TYPE_LABELS) == set(FieldType)
    assert set(FIELD_TYPE_HINTS) == set(FieldType)
    assert all(FIELD_TYPE_LABELS[item] for item in FieldType)
    assert all(FIELD_TYPE_HINTS[item] for item in FieldType)


def test_default_definitions_are_unique_and_valid():
    for record_type in RecordType:
        definitions = default_field_definitions(record_type)
        keys = [item.key for item in definitions]
        assert len(keys) == len(set(keys)), record_type
        for item in definitions:
            assert normalize_field_key(item.key) == item.key
            assert item.label
            assert isinstance(item.field_type, FieldType)


def test_required_fields_by_record_type():
    credentials = required_field_definitions(RecordType.CREDENTIALS)
    assert {item.key for item in credentials} == {"login", "password"}

    assert {item.key for item in required_field_definitions(RecordType.SIP)} == {
        "sip_username",
        "sip_password",
    }
    assert {item.key for item in required_field_definitions(RecordType.LINKS)} == {"url"}
    assert {item.key for item in required_field_definitions(RecordType.INSTRUCTIONS)} == {
        "content"
    }
    assert required_field_definitions(RecordType.OTHER) == ()


def test_validate_required_fields_accepts_filled_values():
    validate_required_fields(
        RecordType.CREDENTIALS,
        {"login": "user", "password": "secret"},
    )
    validate_required_fields(RecordType.OTHER, {})


@pytest.mark.parametrize(
    "record_type,values,missing",
    [
        (RecordType.CREDENTIALS, {"login": "user"}, ("Пароль",)),
        (RecordType.CREDENTIALS, {"login": "  ", "password": "x"}, ("Логин",)),
        (RecordType.SIP, {}, ("SIP Username", "SIP Password")),
        (RecordType.LINKS, {"url": None}, ("Ссылка",)),
        (RecordType.INSTRUCTIONS, {"content": ""}, ("Содержание",)),
    ],
)
def test_validate_required_fields_reports_missing_labels(record_type, values, missing):
    with pytest.raises(MissingRequiredField) as error:
        validate_required_fields(record_type, values)
    assert error.value.labels == missing


# ---------------------------------------------------------------------------
# Валидация значений
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_type,raw,expected",
    [
        (FieldType.TEXT, "  привет  ", "привет"),
        (FieldType.LONG_TEXT, "длинный текст", "длинный текст"),
        (FieldType.PASSWORD, " p@ss word ", " p@ss word "),
        (FieldType.SECRET, "token-123", "token-123"),
        (FieldType.URL, " https://example.com/path?x=1 ", "https://example.com/path?x=1"),
        (FieldType.EMAIL, " User@Example.com ", "User@Example.com"),
        (FieldType.PHONE, " +7 (999) 123-45-67 ", "+7 (999) 123-45-67"),
        (FieldType.NUMBER, "1 234,50", "1234.5"),
        (FieldType.NUMBER, 42, "42"),
        (FieldType.BOOLEAN, "да", "true"),
        (FieldType.BOOLEAN, False, "false"),
        (FieldType.DATE, "15.11.2026", "2026-11-15"),
        (FieldType.DATE, date(2026, 11, 15), "2026-11-15"),
        (FieldType.DATETIME, "15.11.2026 14:30", "2026-11-15T14:30:00"),
        (FieldType.DATETIME, datetime(2026, 11, 15, 14, 30, 5), "2026-11-15T14:30:05"),
        (FieldType.JSON, '{"a": 1, "b": [2, 3]}', {"a": 1, "b": [2, 3]}),
        (FieldType.FILE_REFERENCE, "file-123", "file-123"),
        (
            FieldType.FILE_REFERENCE,
            {"file_id": "abc", "filename": "report.pdf", "extra": "ignored"},
            {"file_id": "abc", "filename": "report.pdf"},
        ),
        (FieldType.MULTISELECT, "alpha, beta, alpha", ["alpha", "beta"]),
        (FieldType.MULTISELECT, ["one", "two"], ["one", "two"]),
    ],
)
def test_validate_field_value_normalizes_valid_input(field_type, raw, expected):
    assert validate_field_value(field_type, raw) == expected


@pytest.mark.parametrize(
    "field_type",
    [
        FieldType.TEXT,
        FieldType.LONG_TEXT,
        FieldType.PASSWORD,
        FieldType.SECRET,
        FieldType.URL,
        FieldType.EMAIL,
        FieldType.PHONE,
        FieldType.NUMBER,
        FieldType.BOOLEAN,
        FieldType.DATE,
        FieldType.DATETIME,
        FieldType.JSON,
        FieldType.FILE_REFERENCE,
        FieldType.MULTISELECT,
    ],
)
def test_validate_field_value_treats_empty_as_none(field_type):
    assert validate_field_value(field_type, None) is None
    assert validate_field_value(field_type, "") is None
    assert validate_field_value(field_type, []) is None


@pytest.mark.parametrize(
    "field_type,raw",
    [
        (FieldType.TEXT, 123),
        (FieldType.TEXT, "x" * 2001),
        (FieldType.LONG_TEXT, "x" * 20001),
        (FieldType.PASSWORD, "x" * 4097),
        (FieldType.URL, "example.com"),
        (FieldType.URL, "ftp://example.com/file"),
        (FieldType.URL, "https://exa mple.com"),
        (FieldType.EMAIL, "not-an-email"),
        (FieldType.EMAIL, "a@b"),
        (FieldType.PHONE, "call-me"),
        (FieldType.PHONE, "123"),
        (FieldType.NUMBER, "abc"),
        (FieldType.NUMBER, "NaN"),
        (FieldType.BOOLEAN, "maybe"),
        (FieldType.DATE, "2026-13-40"),
        (FieldType.DATETIME, "not a date"),
        (FieldType.JSON, "{bad json"),
        (FieldType.FILE_REFERENCE, {"bucket": "no-identifier"}),
        (FieldType.MULTISELECT, ["x" * 65]),
        (
            FieldType.MULTISELECT,
            [f"item-{index}" for index in range(fields.MAX_MULTISELECT_ITEMS + 1)],
        ),
    ],
)
def test_validate_field_value_rejects_invalid_input(field_type, raw):
    with pytest.raises(FieldValidationError):
        validate_field_value(field_type, raw)


def test_parse_multiselect_supports_separators():
    assert parse_multiselect("a, b; c\n d") == ["a", "b", "c", "d"]
    assert parse_multiselect("a, a") == ["a"]
    assert parse_multiselect(None) == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Login", "login"),
        ("  SIP Username ", "sip_username"),
        ("field-1", "field_1"),
        ("2fa", "field_2fa"),
    ],
)
def test_normalize_field_key(raw, expected):
    assert normalize_field_key(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "кириллица", "!!!", "-"])
def test_normalize_field_key_rejects_invalid(raw):
    with pytest.raises(FieldValidationError):
        normalize_field_key(raw)


# ---------------------------------------------------------------------------
# Шифрование, кодирование и маскирование
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field_type", [FieldType.PASSWORD, FieldType.SECRET])
def test_secret_values_are_encrypted(field_type):
    encoded = encode_field_value(field_type, "top-secret")
    assert encoded.value_text is None
    assert encoded.encrypted_value
    assert encoded.encrypted_value != "top-secret"
    assert decrypt_secret(encoded.encrypted_value) == "top-secret"


def test_non_secret_values_are_not_encrypted():
    encoded = encode_field_value(FieldType.TEXT, "plain")
    assert encoded.encrypted_value is None
    assert encoded.value_text == "plain"


def test_secret_mask_and_reveal():
    encoded = encode_field_value(FieldType.PASSWORD, "top-secret")
    assert format_field_value(FieldType.PASSWORD, encrypted_value=encoded.encrypted_value) == (
        SECRET_MASK
    )
    assert (
        format_field_value(
            FieldType.PASSWORD,
            encrypted_value=encoded.encrypted_value,
            reveal=True,
        )
        == "top-secret"
    )
    assert format_field_value(FieldType.SECRET, encrypted_value=None) == "—"


def test_encode_json_and_multiselect_use_json_column():
    json_value = encode_field_value(FieldType.JSON, '{"b": 2, "a": 1}')
    assert json_value.value_json == {"b": 2, "a": 1}
    assert json_value.value_text == '{"b":2,"a":1}'

    selected = encode_field_value(FieldType.MULTISELECT, "one, two")
    assert selected.value_json == ["one", "two"]
    assert selected.value_text == "one, two"


def test_encode_file_reference_keeps_metadata():
    encoded = encode_field_value(
        FieldType.FILE_REFERENCE, {"file_id": "abc", "filename": "report.pdf", "size": 10}
    )
    assert encoded.value_json == {
        "file_id": "abc",
        "filename": "report.pdf",
        "size": 10,
    }
    assert encoded.value_text == "abc"


def test_encode_empty_value_is_empty():
    assert encode_field_value(FieldType.TEXT, "") == EncodedField(raw_value=None)


@pytest.mark.parametrize(
    "field_type,raw,expected",
    [
        (FieldType.TEXT, "text", "text"),
        (FieldType.BOOLEAN, "true", "Да"),
        (FieldType.BOOLEAN, "false", "Нет"),
        (FieldType.DATE, "2026-11-15", "15.11.2026"),
        (FieldType.DATETIME, "2026-11-15T14:30:00", "15.11.2026 14:30"),
        (FieldType.MULTISELECT, ["a", "b"], "a, b"),
        (FieldType.FILE_REFERENCE, {"file_id": "abc", "filename": "report.pdf"}, "report.pdf"),
        (FieldType.NUMBER, None, "—"),
    ],
)
def test_format_raw_field_value(field_type, raw, expected):
    assert format_raw_field_value(field_type, raw) == expected


def test_format_field_value_uses_json_column():
    assert (
        format_field_value(FieldType.JSON, value_json={"a": 1}, value_text='{"a": 1}')
        == '{"a":1}'
    )
    assert format_field_value(FieldType.MULTISELECT, value_json=["a", "b"]) == "a, b"


# ---------------------------------------------------------------------------
# Сервисные валидаторы без БД
# ---------------------------------------------------------------------------


def test_validate_record_title():
    assert validate_record_title("  Запись  ") == "Запись"
    with pytest.raises(RecordValidationError):
        validate_record_title("   ")
    with pytest.raises(RecordValidationError):
        validate_record_title("x" * 257)


def test_validate_record_description():
    assert validate_record_description(None) is None
    assert validate_record_description("  ") is None
    assert validate_record_description("текст") == "текст"
    with pytest.raises(RecordValidationError):
        validate_record_description("x" * 20001)


def test_normalize_access_mode():
    from app.database.models import AccessMode

    assert normalize_access_mode("group") is AccessMode.GROUP
    assert normalize_access_mode(AccessMode.PRIVATE) is AccessMode.PRIVATE
    with pytest.raises(RecordValidationError):
        normalize_access_mode("unknown")


# ---------------------------------------------------------------------------
# FSM, обработчики и длина callback_data
# ---------------------------------------------------------------------------


def test_add_record_states_cover_full_scenario():
    names = {state.state.split(":", 1)[1] for state in AddRecord.__states__}
    assert names == {
        "waiting_type",
        "waiting_title",
        "waiting_description",
        "waiting_fields",
        "waiting_field_name",
        "waiting_field_type",
        "waiting_field_value",
        "waiting_tags",
        "waiting_attachments",
        "waiting_visibility",
        "waiting_groups",
        "preview",
        "confirm",
    }


def test_edit_record_states_cover_fields():
    names = {state.state.split(":", 1)[1] for state in EditRecord.__states__}
    assert names == {
        "waiting_title",
        "waiting_description",
        "waiting_field_name",
        "waiting_field_type",
        "waiting_field_value",
        "waiting_tags",
    }


def test_records_router_exposes_full_add_and_edit_flow():
    callback_names = {
        handler.callback.__name__ for handler in records.router.callback_query.handlers
    }
    message_names = {
        handler.callback.__name__ for handler in records.router.message.handlers
    }
    assert {
        "start_add_record",
        "add_choose_type",
        "add_cancel",
        "add_title",
        "add_description",
        "add_field_value",
        "add_custom_field",
        "add_custom_field_type",
        "add_fields_done",
        "add_tags",
        "add_attachment_document",
        "add_visibility",
        "add_group_toggle",
        "add_preview",
        "add_save",
        "add_confirm",
        "edit_field_start",
        "edit_field_value",
        "edit_add_field_start",
        "edit_title",
        "edit_description",
    } <= callback_names | message_names


def test_field_type_keyboard_callbacks_fit_telegram_limit():
    keyboard = records._type_keyboard("record:ftype", "record:add:back:fields")
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert len(callbacks) == len(FieldType) + 2
    for callback in callbacks:
        assert len(callback.encode()) <= TELEGRAM_CALLBACK_LIMIT


@pytest.mark.parametrize(
    "callback",
    [
        f"record:view:{SAMPLE_UUID}",
        f"record:edit:{SAMPLE_UUID}",
        f"record:edit_title:{SAMPLE_UUID}",
        f"record:edit_desc:{SAMPLE_UUID}",
        f"record:edit_add:{SAMPLE_UUID}",
        f"record:edit_tags:{SAMPLE_UUID}",
        f"record:edit_field:{SAMPLE_UUID}:999",
        f"rr:{SAMPLE_UUID}:999",
        f"record:add:group:{SAMPLE_UUID}",
        "record:add:visibility:ALL_EMPLOYEES",
        "record:add:visibility:BOSSES_ONLY",
        "record:ftype:MULTISELECT",
        "record:etype:MULTISELECT",
        "record:add:INSTRUCTIONS:second_stage",
    ],
)
def test_generated_callbacks_fit_telegram_limit(callback):
    assert len(callback.encode()) <= TELEGRAM_CALLBACK_LIMIT


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
                from app.database.models import Category

                await session.execute(select(Category.id).limit(1))
            except SQLAlchemyError:
                pytest.skip("Apply migrations to the test database first")
            yield session
    finally:
        await engine.dispose()


async def _first_user_and_general_category(session: AsyncSession):
    from app.database.models import Category, User

    user = (await session.execute(select(User).limit(1))).scalar_one_or_none()
    category = (
        await session.execute(select(Category).where(Category.code == "general"))
    ).scalar_one_or_none()
    if user is None or category is None:
        pytest.skip("The test database has no users or the general category")
    return user, category


async def test_create_record_supports_all_field_types(db_session):
    from app.services.records.service import create_record, get_record

    user, _ = await _first_user_and_general_category(db_session)
    fields_payload = {
        "note": {
            "__field__": True,
            "field_type": FieldType.TEXT.value,
            "label": "Заметка",
            "value": "hello",
        },
        "details": {
            "__field__": True,
            "field_type": FieldType.LONG_TEXT.value,
            "label": "Детали",
            "value": "long text",
        },
        "password": {
            "__field__": True,
            "field_type": FieldType.PASSWORD.value,
            "label": "Пароль",
            "value": "top-secret",
        },
        "token": {
            "__field__": True,
            "field_type": FieldType.SECRET.value,
            "label": "Токен",
            "value": "token-value",
        },
        "url": {
            "__field__": True,
            "field_type": FieldType.URL.value,
            "label": "Ссылка",
            "value": "https://example.com",
        },
        "email": {
            "__field__": True,
            "field_type": FieldType.EMAIL.value,
            "label": "Email",
            "value": "user@example.com",
        },
        "phone": {
            "__field__": True,
            "field_type": FieldType.PHONE.value,
            "label": "Телефон",
            "value": "+7 (999) 123-45-67",
        },
        "port": {
            "__field__": True,
            "field_type": FieldType.NUMBER.value,
            "label": "Порт",
            "value": "8080",
        },
        "enabled": {
            "__field__": True,
            "field_type": FieldType.BOOLEAN.value,
            "label": "Включено",
            "value": "да",
        },
        "day": {
            "__field__": True,
            "field_type": FieldType.DATE.value,
            "label": "Дата",
            "value": "15.11.2026",
        },
        "moment": {
            "__field__": True,
            "field_type": FieldType.DATETIME.value,
            "label": "Момент",
            "value": "15.11.2026 14:30",
        },
        "config": {
            "__field__": True,
            "field_type": FieldType.JSON.value,
            "label": "Конфиг",
            "value": '{"timeout": 30}',
        },
        "document": {
            "__field__": True,
            "field_type": FieldType.FILE_REFERENCE.value,
            "label": "Документ",
            "value": {"file_id": "file-1", "filename": "report.pdf"},
        },
        "options": {
            "__field__": True,
            "field_type": FieldType.MULTISELECT.value,
            "label": "Опции",
            "value": "alpha, beta",
        },
    }
    tag_name = f"phase05_{uuid4().hex[:8]}"
    record = await create_record(
        db_session,
        category_code="general",
        record_type=RecordType.OTHER,
        title=f"Phase 05 fields {uuid4().hex[:6]}",
        description="Все типы полей",
        created_by=user,
        fields=fields_payload,
        tags=[tag_name],
        attachments=[{"file_id": "file-2", "filename": "extra.pdf"}],
    )
    try:
        loaded = await get_record(db_session, record.id)
        assert loaded is not None
        by_key = {field.field_key: field for field in loaded.fields}
        assert set(by_key) == set(fields_payload) | {"attachment_1"}

        for key, payload in fields_payload.items():
            assert by_key[key].field_type == payload["field_type"]

        assert by_key["password"].encrypted_value
        assert by_key["password"].value_text is None
        assert decrypt_secret(by_key["password"].encrypted_value) == "top-secret"
        assert format_field_value(
            by_key["password"].field_type,
            encrypted_value=by_key["password"].encrypted_value,
        ) == SECRET_MASK

        assert by_key["port"].value_text == "8080"
        assert by_key["enabled"].value_text == "true"
        assert by_key["day"].value_text == "2026-11-15"
        assert by_key["moment"].value_text == "2026-11-15T14:30:00"
        assert by_key["config"].value_json == {"timeout": 30}
        assert by_key["document"].value_json == {
            "file_id": "file-1",
            "filename": "report.pdf",
        }
        assert by_key["options"].value_json == ["alpha", "beta"]
        assert by_key["attachment_1"].value_json == {
            "file_id": "file-2",
            "filename": "extra.pdf",
        }
        assert [link.tag.name for link in loaded.tags] == [tag_name]
    finally:
        await _drop_record(db_session, record.id)


async def test_create_record_rejects_missing_required_fields(db_session):
    from app.services.records.service import create_record

    user, _ = await _first_user_and_general_category(db_session)
    with pytest.raises(MissingRequiredField):
        await create_record(
            db_session,
            category_code="general",
            record_type=RecordType.CREDENTIALS,
            title="Без пароля",
            description=None,
            created_by=user,
            fields={"login": "user"},
        )


async def test_set_and_delete_record_field(db_session):
    from app.services.records.service import (
        create_record,
        delete_record_field,
        get_record,
        set_record_field,
    )

    user, _ = await _first_user_and_general_category(db_session)
    record = await create_record(
        db_session,
        category_code="general",
        record_type=RecordType.OTHER,
        title=f"Phase 05 edit {uuid4().hex[:6]}",
        description=None,
        created_by=user,
        fields={
            "note": {
                "__field__": True,
                "field_type": FieldType.TEXT.value,
                "label": "Заметка",
                "value": "old",
            }
        },
    )
    try:
        field = next(item for item in record.fields if item.field_key == "note")
        updated = await set_record_field(
            db_session,
            record.id,
            field_id=field.id,
            value="new",
            field_type=FieldType.TEXT,
            user_id=user.id,
        )
        assert updated.value_text == "new"

        created = await set_record_field(
            db_session,
            record.id,
            key="custom_note",
            value="custom",
            field_type=FieldType.TEXT,
            label="Своё поле",
            user_id=user.id,
        )
        assert created.field_key == "custom_note"
        assert created.field_label == "Своё поле"

        await delete_record_field(db_session, record.id, created.id, user_id=user.id)
        loaded = await get_record(db_session, record.id)
        assert loaded is not None
        assert "custom_note" not in {item.field_key for item in loaded.fields}
    finally:
        await _drop_record(db_session, record.id)


async def _drop_record(session: AsyncSession, record_id: UUID) -> None:
    from app.database.models import Record, RecordField, RecordPermission, RecordTag

    await session.execute(delete(RecordField).where(RecordField.record_id == record_id))
    await session.execute(delete(RecordTag).where(RecordTag.record_id == record_id))
    await session.execute(
        delete(RecordPermission).where(RecordPermission.record_id == record_id)
    )
    await session.execute(delete(Record).where(Record.id == record_id))
    await session.commit()
