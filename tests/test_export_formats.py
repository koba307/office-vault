"""Форматы экспорта Phase 08: CSV, JSON, XLSX, ZIP, выбор полей и сборка таблицы.

Тесты не требуют базы данных: записи собираются в памяти.
"""

import csv
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from xml.etree import ElementTree

import pytest

from app.core.encryption import encrypt_secret
from app.database.models import Category, Record, RecordField, RecordTag, RecordType, Tag
from app.services.import_export.export import (
    ExportValidationError,
    build_export_filename,
    normalize_format,
    parse_record_ids,
    parse_record_ids_text,
)
from app.services.import_export.export_formats import (
    FORMAT_EXTENSIONS,
    FORMAT_MIME_TYPES,
    csv_bytes,
    json_bytes,
    neutralize_cell,
    xlsx_bytes,
)
from app.services.import_export.export_jobs import export_expires_at
from app.services.import_export.rows import (
    ATTACHMENTS_COLUMN,
    BASE_COLUMNS,
    ColumnSelectionError,
    ExportTable,
    build_table,
)
from app.services.import_export.xlsx_writer import build_xlsx, column_letter
from app.services.import_export.zip_export import (
    MANIFEST_MEMBER,
    RECORDS_MEMBER,
    assemble_zip,
    build_manifest,
    checksum,
)

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _category(code: str = "holodka", name: str = "Холодка") -> Category:
    return Category(id=uuid4(), code=code, name=name, category_type="root", status="ACTIVE")


def _record(
    *,
    title: str = "Тест",
    record_type: RecordType = RecordType.CREDENTIALS,
    category: Category | None = None,
    fields: list[RecordField] | None = None,
    tags: tuple[str, ...] = (),
) -> Record:
    record = Record(
        id=uuid4(),
        category=category or _category(),
        record_type=record_type,
        title=title,
        description="Описание",
        status="ACTIVE",
        created_by=uuid4(),
    )
    record.created_at = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    record.fields = fields or []
    record.tags = [RecordTag(tag=Tag(name=name)) for name in tags]
    return record


def _login_field(value: str = "ivan") -> RecordField:
    return RecordField(
        field_key="login",
        field_label="Логин",
        field_type="TEXT",
        value_text=value,
        sort_order=0,
    )


def _password_field(secret: str) -> RecordField:
    return RecordField(
        field_key="password",
        field_label="Пароль",
        field_type="PASSWORD",
        encrypted_value=encrypt_secret(secret),
        sort_order=1,
    )


def _xlsx_cells(payload: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in sheet.findall(".//s:sheetData/s:row", NS):
        values = []
        for cell in row.findall("s:c", NS):
            text = cell.find("s:is/s:t", NS)
            values.append(text.text or "" if text is not None else "")
        rows.append(values)
    return rows


# ---------------------------------------------------------------------------
# Нейтрализация формул и CSV
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("=HYPERLINK(1)", "'=HYPERLINK(1)"),
        ("+7 999 123", "'+7 999 123"),
        ("-5", "'-5"),
        ("@cmd", "'@cmd"),
        ("обычный текст", "обычный текст"),
        (42, 42),
        (None, None),
    ],
)
def test_neutralize_cell_escapes_formulas(raw, expected):
    assert neutralize_cell(raw) == expected


def test_csv_has_bom_header_and_neutralized_cells():
    table = ExportTable(
        columns=("id", "title"),
        rows=({"id": "1", "title": "=evil()"}, {"id": "2", "title": "Пароли"}),
    )

    payload = csv_bytes(table)

    assert payload.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
    assert rows == [["id", "title"], ["1", "'=evil()"], ["2", "Пароли"]]


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_json_document_roundtrip_keeps_columns_and_values():
    table = ExportTable(
        columns=("id", "title"),
        rows=({"id": "1", "title": "Почта"},),
    )
    generated_at = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

    document = json.loads(json_bytes(table, generated_at=generated_at))

    assert document["format"] == "office-vault-export"
    assert document["version"] == 1
    assert document["columns"] == ["id", "title"]
    assert document["record_count"] == 1
    assert document["records"] == [{"id": "1", "title": "Почта"}]
    assert document["generated_at"] == generated_at.isoformat()


def test_json_does_not_neutralize_values():
    table = ExportTable(columns=("title",), rows=({"title": "=x"},))

    document = json.loads(json_bytes(table, generated_at=datetime.now(UTC)))

    assert document["records"][0]["title"] == "=x"


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------


def test_xlsx_contains_header_and_escaped_values():
    table = ExportTable(
        columns=("title", "note"),
        rows=({"title": "A & B <tag>", "note": "x\x01y"},),
    )

    payload = xlsx_bytes(table)

    cells = _xlsx_cells(payload)
    assert cells[0] == ["title", "note"]
    assert cells[1] == ["A & B <tag>", "xy"]


def test_xlsx_is_a_valid_package_with_required_parts():
    payload = build_xlsx(["a"], [["1"]])

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
        content_types = archive.read("[Content_Types].xml").decode("utf-8")
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))

    assert {
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/worksheets/sheet1.xml",
    } <= names
    assert "spreadsheetml.sheet.main+xml" in content_types
    assert workbook.find(".//s:sheet", NS) is not None


def test_xlsx_neutralizes_formulas_in_cells():
    table = ExportTable(columns=("title",), rows=({"title": "=1+1"},))

    cells = _xlsx_cells(xlsx_bytes(table))

    assert cells[1] == ["'=1+1"]


@pytest.mark.parametrize(
    ("index", "letters"),
    [(0, "A"), (25, "Z"), (26, "AA"), (701, "ZZ"), (702, "AAA")],
)
def test_column_letter(index, letters):
    assert column_letter(index) == letters


def test_xlsx_truncates_long_cells():
    cells = _xlsx_cells(build_xlsx(["t"], [["я" * 40000]]))

    assert len(cells[1][0]) == 32767


# ---------------------------------------------------------------------------
# Сборка таблицы: колонки, выбор полей, маскирование
# ---------------------------------------------------------------------------


def test_build_table_default_columns_include_base_and_dynamic_fields():
    record = _record(fields=[_login_field(), _password_field("s3cret")])

    table = build_table([record])

    assert table.columns == (*BASE_COLUMNS, "login", "password")
    row = table.rows[0]
    assert row["title"] == "Тест"
    assert row["category"] == "holodka"
    assert row["type"] == "CREDENTIALS"
    assert row["login"] == "ivan"
    assert row["created_at"] == "2026-11-15T10:00:00+00:00"


def test_build_table_masks_secrets_by_default():
    record = _record(fields=[_password_field("hunter2-very-secret")])

    table = build_table([record])

    assert table.rows[0]["password"] == "***"
    assert "hunter2-very-secret" not in json.dumps(table.rows, ensure_ascii=False)


def test_build_table_decrypts_secrets_only_when_requested():
    record = _record(fields=[_password_field("hunter2-very-secret")])

    table = build_table([record], include_secrets=True)

    assert table.rows[0]["password"] == "hunter2-very-secret"


def test_build_table_respects_field_selection_order():
    record = _record(fields=[_login_field()], tags=("почта",))

    table = build_table([record], fields=["tags", "title", "login"])

    assert table.columns == ("tags", "title", "login")
    assert table.rows[0] == {"tags": "почта", "title": "Тест", "login": "ivan"}


def test_build_table_rejects_unknown_fields():
    record = _record(fields=[_login_field()])

    with pytest.raises(ColumnSelectionError, match="Неизвестные поля"):
        build_table([record], fields=["title", "nonexistent"])


def test_build_table_rejects_empty_selection():
    with pytest.raises(ColumnSelectionError):
        build_table([_record()], fields=["  "])


def test_build_table_deduplicates_selected_fields():
    table = build_table([_record()], fields=["title", "title", "type"])

    assert table.columns == ("title", "type")


def test_attachments_column_added_when_files_enabled_even_with_explicit_fields():
    record = _record()
    attachments = {record.id: ["отчёт.pdf", "схема.png"]}

    table = build_table([record], fields=["title"], attachments=attachments)

    assert table.columns == ("title", ATTACHMENTS_COLUMN)
    assert table.rows[0][ATTACHMENTS_COLUMN] == "отчёт.pdf, схема.png"


def test_attachments_column_absent_without_attachments_mapping():
    table = build_table([_record()])

    assert ATTACHMENTS_COLUMN not in table.columns


# ---------------------------------------------------------------------------
# Форматы, имена файлов, идентификаторы
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["csv", "CSV", " xlsx ", "json", "zip"])
def test_normalize_format_accepts_known_formats(raw):
    assert normalize_format(raw) in FORMAT_EXTENSIONS


@pytest.mark.parametrize("raw", ["exe", "", None, "pdf"])
def test_normalize_format_rejects_unknown_formats(raw):
    with pytest.raises(ExportValidationError):
        normalize_format(raw)


def test_every_format_has_extension_and_mime():
    assert set(FORMAT_EXTENSIONS) == set(FORMAT_MIME_TYPES)


def test_export_filename_depends_only_on_service_values():
    moment = datetime(2026, 11, 15, 9, 30, 5, tzinfo=UTC)

    name = build_export_filename("xlsx", moment=moment, include_secrets=False)
    secret_name = build_export_filename("zip", moment=moment, include_secrets=True)

    assert name == "office_vault_export_20261115_093005.xlsx"
    assert secret_name == "office_vault_secrets_export_20261115_093005.zip"


def test_parse_record_ids_deduplicates_and_validates():
    first = uuid4()

    assert parse_record_ids([str(first), first]) == [first]
    assert parse_record_ids(None) is None


def test_parse_record_ids_rejects_garbage():
    with pytest.raises(ExportValidationError):
        parse_record_ids(["не-uuid"])


def test_parse_record_ids_text_accepts_separators():
    first, second = uuid4(), uuid4()
    text = f"{first},\n{second};  "

    assert parse_record_ids_text(text) == [first, second]


def test_parse_record_ids_text_requires_value():
    with pytest.raises(ExportValidationError):
        parse_record_ids_text("   ")


# ---------------------------------------------------------------------------
# ZIP: сборка, манифест, контрольные суммы
# ---------------------------------------------------------------------------


def test_assemble_zip_keeps_members_in_order():
    payload = assemble_zip([("records.json", b"[]"), ("files/a.txt", b"hello")])

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == ["records.json", "files/a.txt"]
        assert archive.read("files/a.txt") == b"hello"


@pytest.mark.parametrize("name", ["../evil", "/etc/passwd", "files/../../x"])
def test_assemble_zip_rejects_unsafe_member_names(name):
    with pytest.raises(ValueError):
        assemble_zip([(name, b"x")])


def test_assemble_zip_rejects_duplicate_members():
    with pytest.raises(ValueError):
        assemble_zip([("a", b"1"), ("a", b"2")])


def test_build_manifest_reports_totals_and_sorted_checksums():
    generated_at = datetime(2026, 11, 15, tzinfo=UTC)
    manifest = build_manifest(
        generated_at=generated_at,
        records=[{"id": "r1"}],
        files=[{"file_id": "f1"}],
        skipped_files=[{"file_id": "f2", "reason": "нет права"}],
        include_files=True,
        include_secrets=False,
        checksums={"files/b.bin": "bb", "records.json": "aa"},
    )

    assert manifest["totals"] == {"records": 1, "files": 1, "skipped_files": 1}
    assert list(manifest["checksums"]) == ["files/b.bin", "records.json"]
    assert manifest["include_secrets"] is False
    assert manifest["generated_at"] == generated_at.isoformat()


def test_zip_members_constants_are_stable():
    assert RECORDS_MEMBER == "records.json"
    assert MANIFEST_MEMBER == "manifest.json"


def test_checksum_is_sha256_hex():
    assert checksum(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


# ---------------------------------------------------------------------------
# Срок хранения
# ---------------------------------------------------------------------------


def test_export_expires_after_retention_window():
    finished = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)

    assert export_expires_at(finished, retention_hours=24) == finished + timedelta(hours=24)
