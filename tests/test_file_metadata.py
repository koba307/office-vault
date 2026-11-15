"""Тесты метаданных файлов Phase 06: SHA-256, MIME, расширения, имена, лимиты."""

import pytest

from app.database.models import ScanStatus
from app.services.files.metadata import (
    BLOCKED_EXTENSIONS,
    GENERIC_MIME_TYPE,
    calculate_sha256,
    check_size_limit,
    detect_mime_type,
    get_extension,
    normalize_mime_type,
    sanitize_filename,
    validate_extension,
)
from app.services.files.service import (
    FileExtensionMismatch,
    FileMimeMismatch,
    FileTooLarge,
    FileValidationError,
    prepare_upload,
)

# ---------------------------------------------------------------------------
# SHA-256
# ---------------------------------------------------------------------------


def test_sha256_known_vectors():
    assert calculate_sha256(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert calculate_sha256(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_sha256_accepts_bytearray_and_memoryview():
    digest = calculate_sha256(bytearray(b"abc"))
    assert digest == calculate_sha256(memoryview(b"abc"))


def test_sha256_changes_with_content():
    assert calculate_sha256(b"file") != calculate_sha256(b"file ")


# ---------------------------------------------------------------------------
# MIME по magic bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data,filename,expected",
    [
        (b"%PDF-1.7\n...", "document.pdf", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n" + b"data", "image.png", "image/png"),
        (b"\xff\xd8\xff\xe0" + b"data", "photo.jpg", "image/jpeg"),
        (b"GIF89a" + b"data", "anim.gif", "image/gif"),
        (b"BM" + b"\x00" * 12, "bitmap.bmp", "image/bmp"),
        (b"PK\x03\x04" + b"data", "archive.zip", "application/zip"),
        (
            b"PK\x03\x04" + b"data",
            "contract.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            b"PK\x03\x04" + b"data",
            "table.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"data", "old.doc", "application/msword"),
        (b"\x1f\x8b" + b"data", "backup.tar.gz", "application/gzip"),
        (b"7z\xbc\xaf\x27\x1c" + b"data", "backup.7z", "application/x-7z-compressed"),
        (b"Rar!\x1a\x07" + b"data", "backup.rar", "application/vnd.rar"),
        (b"RIFF" + b"\x00" * 4 + b"WAVE" + b"fmt ", "sound.wav", "audio/wav"),
        (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8 ", "image.webp", "image/webp"),
        (b"\x00\x00\x00\x18ftypmp42" + b"data", "video.mp4", "video/mp4"),
        (b"OggS" + b"data", "sound.ogg", "audio/ogg"),
        (b"ID3" + b"data", "song.mp3", "audio/mpeg"),
        (b"SQLite format 3\x00" + b"data", "data.db", "application/vnd.sqlite3"),
    ],
)
def test_detect_mime_by_magic_bytes(data, filename, expected):
    assert detect_mime_type(data, filename) == expected


@pytest.mark.parametrize(
    "data,filename,expected",
    [
        (b'{"key": "value"}', "data.json", "application/json"),
        (b"key,value\n1,2\n", "table.csv", "text/csv"),
        (b"a\tb\n1\t2\n", "table.tsv", "text/tab-separated-values"),
        (b'<?xml version="1.0"?><root/>', "data.xml", "application/xml"),
        (b"<html><body>ok</body></html>", "page.html", "text/html"),
        ("# Заголовок\n\nТекст".encode(), "readme.md", "text/markdown"),
        (b"key: value\n", "config.yaml", "application/yaml"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', "icon.svg", "image/svg+xml"),
        ("Просто текст".encode(), "note.txt", "text/plain"),
        ("Просто текст".encode(), "README", "text/plain"),
    ],
)
def test_detect_text_formats(data, filename, expected):
    assert detect_mime_type(data, filename) == expected


def test_detect_unknown_binary_is_octet_stream():
    assert detect_mime_type(b"\x00\x01\x02\x03binary", "payload.bin") == GENERIC_MIME_TYPE


def test_detect_empty_file_is_octet_stream():
    assert detect_mime_type(b"", "empty.bin") == GENERIC_MIME_TYPE


def test_detect_does_not_trust_png_extension_for_text():
    assert detect_mime_type(b"plain text", "fake.png") == "text/plain"


# ---------------------------------------------------------------------------
# Нормализация MIME и расширение
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("image/jpg", "image/jpeg"),
        ("application/x-pdf", "application/pdf"),
        ("text/xml", "application/xml"),
        ("TEXT/PLAIN; charset=utf-8", "text/plain"),
        (None, GENERIC_MIME_TYPE),
        ("", GENERIC_MIME_TYPE),
    ],
)
def test_normalize_mime_type(value, expected):
    assert normalize_mime_type(value) == expected


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("report.pdf", "pdf"),
        ("REPORT.PDF", "pdf"),
        ("archive.tar.gz", "gz"),
        (".env", "env"),
        ("README", None),
        ("", None),
        ("../../etc/passwd", None),
        ("file.", None),
    ],
)
def test_get_extension(filename, expected):
    assert get_extension(filename) == expected


@pytest.mark.parametrize(
    "mime_type,filename,expected",
    [
        ("application/pdf", "report.pdf", True),
        ("application/pdf", "report.txt", False),
        ("image/png", "photo.png", True),
        ("image/png", "photo.jpg", False),
        ("image/jpeg", "photo.JPG", True),
        ("text/plain", "notes.txt", True),
        ("text/plain", "README", True),
        ("application/json", "data.json", True),
        ("application/zip", "contract.docx", True),
        ("application/zip", "archive.rar", False),
        ("application/x-ole-storage", "old.xls", True),
        ("application/pdf", "no_extension", False),
        ("application/pdf", "virus.exe", False),
        ("application/octet-stream", "payload.bin", True),
        ("application/octet-stream", "script.ps1", False),
    ],
)
def test_validate_extension(mime_type, filename, expected):
    assert validate_extension(mime_type, filename) is expected


def test_blocked_extensions_are_never_valid():
    for extension in BLOCKED_EXTENSIONS:
        assert validate_extension(GENERIC_MIME_TYPE, f"file.{extension}") is False


# ---------------------------------------------------------------------------
# Безопасные имена
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
        ("/absolute/path/file.txt", "file.txt"),
        ("a\x00b.txt", "ab.txt"),
        ("file\nname.txt", "filename.txt"),
        ("файл отчёт 2026.pdf", "файл отчёт 2026.pdf"),
        ("", "file"),
        ("..", "file"),
        (".", "file"),
        ("CON.txt", "_CON.txt"),
        ("bad:name?.txt", "bad_name_.txt"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_removes_directory_components():
    result = sanitize_filename("a/b/../../c.txt")
    assert "/" not in result
    assert "\\" not in result
    assert result == "c.txt"


def test_sanitize_filename_keeps_extension_when_truncating():
    result = sanitize_filename("a" * 400 + ".pdf")
    assert len(result) <= 255
    assert result.endswith(".pdf")


def test_sanitize_filename_handles_none():
    assert sanitize_filename(None) == "file"


# ---------------------------------------------------------------------------
# Лимит размера
# ---------------------------------------------------------------------------


def test_check_size_limit_boundaries():
    assert check_size_limit(b"12345", 5) is True
    assert check_size_limit(b"123456", 5) is False
    assert check_size_limit(b"", 0) is True
    assert check_size_limit(b"", -1) is False


# ---------------------------------------------------------------------------
# prepare_upload: связка проверок
# ---------------------------------------------------------------------------


def test_prepare_upload_returns_sanitized_metadata():
    prepared = prepare_upload(b"%PDF-1.7\ncontent", "../../договор.pdf", "application/pdf")
    assert prepared.filename == "договор.pdf"
    assert prepared.mime_type == "application/pdf"
    assert prepared.size == len(b"%PDF-1.7\ncontent")
    assert len(prepared.sha256) == 64


def test_prepare_upload_rejects_empty_file():
    with pytest.raises(FileValidationError, match="пуст"):
        prepare_upload(b"", "empty.txt", "text/plain")


def test_prepare_upload_rejects_oversized_file():
    with pytest.raises(FileTooLarge):
        prepare_upload(b"12345", "a.txt", "text/plain", max_size=4)


def test_prepare_upload_rejects_mime_mismatch():
    with pytest.raises(FileMimeMismatch):
        prepare_upload(b"%PDF-1.7\ncontent", "document.pdf", "image/png")


def test_prepare_upload_rejects_extension_mismatch():
    with pytest.raises(FileExtensionMismatch):
        prepare_upload(b"%PDF-1.7\ncontent", "document.txt", "application/pdf")


def test_prepare_upload_accepts_generic_content_type():
    prepared = prepare_upload(b"%PDF-1.7\ncontent", "document.pdf", None)
    assert prepared.mime_type == "application/pdf"


def test_prepare_upload_accepts_zip_container_for_office_file():
    prepared = prepare_upload(b"PK\x03\x04content", "contract.docx", "application/zip")
    assert prepared.mime_type.endswith("wordprocessingml.document")


# ---------------------------------------------------------------------------
# Отображение метаданных в боте
# ---------------------------------------------------------------------------


def test_upload_metadata_text_contains_sha256_mime_and_scan_status():
    from app.bot.handlers.files import _upload_metadata_text
    from app.database.models import File

    file = File(
        original_name="договор.pdf",
        storage_key="files/1/договор.pdf",
        bucket="office-vault",
        mime_type="application/pdf",
        size=2048,
        sha256="a" * 64,
        scan_status=ScanStatus.CLEAN,
        uploaded_by=None,
    )

    text = _upload_metadata_text(file)

    assert "договор.pdf" in text
    assert "application/pdf" in text
    assert "a" * 64 in text
    assert "чисто" in text
    assert "карантине" not in text


def test_upload_metadata_text_warns_about_quarantine():
    from app.bot.handlers.files import _upload_metadata_text
    from app.database.models import File

    file = File(
        original_name="virus.bin",
        storage_key="quarantine/1/virus.bin",
        bucket="office-vault",
        mime_type="application/octet-stream",
        size=10,
        sha256="b" * 64,
        scan_status=ScanStatus.PENDING,
        uploaded_by=None,
    )

    text = _upload_metadata_text(file)

    assert "карантине" in text
    assert "ожидает проверки" in text
