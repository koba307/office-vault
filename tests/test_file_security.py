"""Тесты файловой безопасности Phase 06.

Проверяют модуль app/services/files/security.py (path traversal, ZIP bomb,
magic bytes) и его интеграцию в app.services.files.service.inspect_upload.
Чистые unit-тесты: база и хранилище не требуются.
"""

import io
import zipfile
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from app.database.models import File, ScanStatus
from app.services.files import service
from app.services.files.security import (
    DEFAULT_ZIP_MAX_ENTRIES,
    DEFAULT_ZIP_MAX_RATIO,
    FALLBACK_FILENAME,
    MAX_FILENAME_LENGTH,
    ZipBombResult,
    check_zip_bomb,
    detect_magic_mime,
    sanitize_path,
    validate_magic_bytes,
)
from app.services.files.service import FileSecurityError, FileTooLargeError, inspect_upload

# ---------------------------------------------------------------------------
# Санкти-данные
# ---------------------------------------------------------------------------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff\xe0"
PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\ncontent"


def _make_zip(entries: dict[str, bytes], *, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# sanitize_path: path traversal и мусорные имена
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("/var/tmp/report.pdf", "report.pdf"),
        ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
        ("....//....//secret.txt", "secret.txt"),
        ("C:\\Users\\boss\\Desktop\\акт.pdf", "акт.pdf"),
        ("docs/../..", "unnamed"),
        ("a\x00b.txt", "ab.txt"),
        ("bad\r\nname.txt", "badname.txt"),
        ("", FALLBACK_FILENAME),
        (None, FALLBACK_FILENAME),
        (".", FALLBACK_FILENAME),
        ("..", FALLBACK_FILENAME),
        ("/", FALLBACK_FILENAME),
        ("...", FALLBACK_FILENAME),
        ("con", FALLBACK_FILENAME),
        ("NUL.txt", FALLBACK_FILENAME),
        ("com1.log", FALLBACK_FILENAME),
        ("   spaced   name.txt   ", "spaced name.txt"),
        ("weird:file*name?.txt", "weird_file_name_.txt"),
    ],
)
def test_sanitize_path_neutralizes_hostile_names(raw, expected):
    assert sanitize_path(raw) == expected


def test_sanitize_path_keeps_harmless_names():
    assert sanitize_path("Договор поставки №3 (2026).pdf") == "Договор поставки №3 (2026).pdf"
    assert sanitize_path("report-2026_final.V2.XLSX") == "report-2026_final.V2.XLSX"


def test_sanitize_path_result_has_no_separators_or_traversal():
    hostile = ["a/../../b", "..\\..\\x", "\x00../../etc/shadow", "///", "....\\....\\x"]
    for raw in hostile:
        result = sanitize_path(raw)
        assert "/" not in result
        assert "\\" not in result
        assert ".." not in result
        assert "\x00" not in result


def test_sanitize_path_truncates_but_keeps_extension():
    result = sanitize_path("x" * 400 + ".pdf")
    assert len(result) <= MAX_FILENAME_LENGTH
    assert result.endswith(".pdf")


# ---------------------------------------------------------------------------
# check_zip_bomb
# ---------------------------------------------------------------------------


def test_zip_bomb_skips_non_zip_content():
    result = check_zip_bomb(b"just a plain text file")
    assert isinstance(result, ZipBombResult)
    assert result.safe is True
    assert result.is_zip is False


def test_zip_bomb_skips_empty_input():
    result = check_zip_bomb(b"")
    assert result.safe is True
    assert result.is_zip is False


def test_zip_bomb_accepts_small_normal_archive():
    payload = ("нормальный текст документа " * 20).encode("utf-8")
    data = _make_zip({"note.txt": payload})
    result = check_zip_bomb(data)
    assert result.safe is True
    assert result.is_zip is True
    assert result.entries == 1
    assert result.uncompressed_size == len(payload)


def test_zip_bomb_accepts_empty_archive():
    data = _make_zip({})
    result = check_zip_bomb(data)
    assert result.safe is True
    assert result.entries == 0


def test_zip_bomb_detects_high_compression_ratio():
    # 8 MiB нулей сжимаются почти в ноль: ratio заведомо выше дефолтного лимита.
    data = _make_zip({"huge.bin": b"\x00" * (8 * 1024 * 1024)})
    result = check_zip_bomb(data)
    assert result.safe is False
    assert result.is_zip is True
    assert result.ratio > DEFAULT_ZIP_MAX_RATIO
    assert result.reason is not None


def test_zip_bomb_detects_declared_size_over_limit():
    data = _make_zip({"big.bin": b"mixed" * (2 * 1024 * 1024)})  # ~10 MiB declared
    result = check_zip_bomb(data, max_ratio=10_000, max_size=1024)
    assert result.safe is False
    assert "лимит" in (result.reason or "")


def test_zip_bomb_custom_ratio_parameter_is_honored():
    data = _make_zip({"a.txt": b"a" * (512 * 1024)})
    lenient = check_zip_bomb(data, max_ratio=10_000)
    strict = check_zip_bomb(data, max_ratio=1.5)
    assert lenient.safe is True
    assert strict.safe is False


def test_zip_bomb_stored_archive_has_neutral_ratio():
    data = _make_zip({"raw.bin": b"1234567890" * 1000}, compression=zipfile.ZIP_STORED)
    result = check_zip_bomb(data)
    assert result.safe is True
    # ZIP_STORED: распакованный ≈ упакованному (заголовки добавляют сотые доли).
    assert 0.9 <= result.ratio <= 1.1


def test_zip_bomb_rejects_corrupted_zip_with_valid_magic():
    result = check_zip_bomb(b"PK\x03\x04" + b"\xff" * 64)
    assert result.safe is False
    assert result.is_zip is True
    assert result.reason is not None


def test_zip_max_entries_constant_exists_for_catalog_explosion():
    # Защита от «тысячи мелких записей»: константа используется в проверке.
    assert DEFAULT_ZIP_MAX_ENTRIES > 0


# ---------------------------------------------------------------------------
# validate_magic_bytes / detect_magic_mime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected_mime"),
    [
        (PNG_MAGIC + b"xyz", "image/png"),
        (JPEG_MAGIC + b"xyz", "image/jpeg"),
        (b"GIF89a....", "image/gif"),
        (PDF_BYTES, "application/pdf"),
        (b"PK\x03\x04minizip", "application/zip"),
        (b"\x1f\x8b\x08gzip", "application/gzip"),
        (b"7z\xbc\xaf\x27\x1cxx", "application/x-7z-compressed"),
        (b"Rar!\x1a\x07\x01", "application/vnd.rar"),
        (b"BM....", "image/bmp"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1ole", "application/x-ole-storage"),
        (b"MZ\x90\x00exe", "application/x-dosexec"),
        (b"\x7fELF\x02bin", "application/x-executable"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"\x00" * 257 + b"ustar\x00", "application/x-tar"),
    ],
)
def test_detect_magic_mime_recognizes_signatures(payload, expected_mime):
    assert detect_magic_mime(payload) == expected_mime


def test_detect_mime_returns_none_for_unknown_and_empty():
    assert detect_magic_mime(b"\x01\x02\x03 random binary blob") is None
    assert detect_magic_mime(b"") is None


def test_validate_accepts_matching_signature():
    assert validate_magic_bytes(PNG_MAGIC + b"data", "image/png") is True
    assert validate_magic_bytes(PDF_BYTES, "application/pdf") is True
    assert validate_magic_bytes(PDF_BYTES, "APPLICATION/PDF; charset=binary") is True


def test_validate_rejects_spoofed_container():
    # ZIP-архив, притворяющийся PNG.
    assert validate_magic_bytes(b"PK\x03\x04zipdata", "image/png") is False
    # PNG-байты, заявленные как JPEG.
    assert validate_magic_bytes(PNG_MAGIC + b"x", "image/jpeg") is False


def test_validate_is_permissive_for_generic_and_unknown_cases():
    assert validate_magic_bytes(b"PK\x03\x04zip", "application/octet-stream") is True
    assert validate_magic_bytes(b"PK\x03\x04zip", None) is True
    assert validate_magic_bytes(b"\x01\x02\x03", "image/png") is True  # сигнатура не опровергает
    assert validate_magic_bytes(b"col1,col2\n1,2\n", "text/csv") is True


def test_validate_allows_office_container_formats():
    docx_mime = (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document"
    )
    assert validate_magic_bytes(b"PK\x03\x04word/", docx_mime) is True
    assert validate_magic_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1ole", "application/msword") is True


# ---------------------------------------------------------------------------
# inspect_upload: связка проверок в сервисе
# ---------------------------------------------------------------------------


def test_inspect_upload_returns_normalized_metadata():
    inspection = inspect_upload(PDF_BYTES, "../../договор.pdf", "application/pdf")
    assert inspection.safe_name == "договор.pdf"
    assert inspection.content_type == "application/pdf"
    assert inspection.extension == "pdf"
    assert inspection.size == len(PDF_BYTES)
    assert inspection.sha256 == sha256(PDF_BYTES).hexdigest()


def test_inspect_upload_falls_back_to_detected_mime():
    inspection = inspect_upload(_make_zip({"a.txt": b"hello"}), "archive.zip", None)
    assert inspection.content_type == "application/zip"
    assert inspection.safe_name == "archive.zip"


def test_inspect_upload_rejects_empty_file():
    with pytest.raises(FileSecurityError):
        inspect_upload(b"", "empty.txt", "text/plain")


def test_inspect_upload_rejects_oversized_file(monkeypatch):
    monkeypatch.setattr(service.settings, "max_upload_bytes", 10)
    with pytest.raises(FileTooLargeError):
        inspect_upload(b"0" * 11, "a.txt", "text/plain")


def test_inspect_upload_rejects_zip_bomb():
    bomb = _make_zip({"huge.bin": b"\x00" * (8 * 1024 * 1024)})
    with pytest.raises(FileSecurityError, match="сжат"):
        inspect_upload(bomb, "photos.zip", "application/zip")


def test_inspect_upload_rejects_mime_spoof():
    # Валидный ZIP-архив, заявленный как PNG: отсекается по magic bytes.
    with pytest.raises(FileSecurityError, match="не соответствует"):
        inspect_upload(_make_zip({"secret": b"data"}), "evil.png", "image/png")


def test_inspect_upload_zip_limits_follow_settings(monkeypatch):
    monkeypatch.setattr(service.settings, "zip_max_ratio", 1.0)
    monkeypatch.setattr(service.settings, "zip_max_uncompressed", 10)
    assert service.zip_bomb_limits() == (1.0, 10)


def test_inspect_upload_never_leaks_traversal_into_metadata():
    inspection = inspect_upload(PDF_BYTES, "/etc/cron.d/evil%00.pdf", "application/pdf")
    assert "/" not in inspection.safe_name
    assert "\x00" not in inspection.safe_name


# ---------------------------------------------------------------------------
# Граница безопасности в UI: имена файлов и callback-адреса
# ---------------------------------------------------------------------------

CALLBACK_LIMIT = 64


def _deleted_file(original_name: str = "акт.pdf") -> File:
    return File(
        id=uuid4(),
        original_name=original_name,
        storage_key="files/x/файл",
        bucket="office-vault",
        mime_type="application/pdf",
        extension="pdf",
        size=10,
        sha256="b" * 64,
        scan_status=ScanStatus.CLEAN,
        uploaded_by=uuid4(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        deleted_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


def test_file_archive_keyboard_escapes_hostile_names_and_fits_limits():
    from app.bot.handlers.archive import build_file_archive_rows, file_archive_view_text

    hostile = "<b>взлом</b>.pdf"
    file = _deleted_file(hostile)
    rows = build_file_archive_rows([file], can_restore=True, can_purge=True)
    for row in rows:
        for button in row:
            assert len(button.callback_data.encode()) <= CALLBACK_LIMIT
    # Имя в тексте карточки экранируется, а не вшивается как HTML.
    text = file_archive_view_text(file, "Автор")
    assert "&lt;b&gt;взлом&lt;/b&gt;.pdf" in text
    assert "<b>взлом</b>" not in text


def test_file_archive_rows_hide_actions_without_permissions():
    from app.bot.handlers.archive import build_file_archive_rows

    rows = build_file_archive_rows([_deleted_file()], can_restore=False, can_purge=False)
    callbacks = [btn.callback_data for row in rows for btn in row]
    assert len(callbacks) == 1
    assert callbacks[0].startswith("archive:file:")


def test_lifecycle_callbacks_do_not_shadow_each_other():
    # «files:delete_yes:» не должен перехватываться фильтром «files:delete:».
    file_id = uuid4()
    confirm = f"files:delete_yes:{file_id}"
    assert not confirm.startswith("files:delete:")
    undelete = f"files:undelete:{file_id}"
    assert not undelete.startswith("files:delete:")
    purge_yes = f"archive:file_purge_yes:{file_id}"
    assert not purge_yes.startswith("archive:file_purge:")
    assert not purge_yes.startswith("archive:file:")
    assert not "archive:files".startswith("archive:file:")
    for callback in (confirm, undelete, purge_yes):
        assert len(callback.encode()) <= CALLBACK_LIMIT
