"""Файловые проверки безопасности (Phase 06).

Три уровня защиты при загрузке и скачивании файлов:

1. ``sanitize_path`` — нормализация имени файла: защита от path traversal,
   control-символов, вложенных каталогов и «мусорных» имён.
2. ``check_zip_bomb`` — проверка ZIP-архивов на «бомбы»: сильное сжатие
   (ratio), раздутый декларируемый размер и избыточное число записей.
3. ``validate_magic_bytes`` — сопоставление реальных magic bytes с
   заявленным MIME-типом: файл не должен притворяться другим форматом.

Модуль не обращается к БД и хранилищу — только чистые функции над байтами.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Настройки по умолчанию (переопределяются аргументами функций)
# ---------------------------------------------------------------------------

# Максимальное «здоровое» сжатие: текст и бинарные данные обычно дают 10x–30x.
# 100x — заведомая аномалия, типичный признак bomb.
DEFAULT_ZIP_MAX_RATIO = 100.0
# Декларируемый максимальный распакованный размер архива, байты (200 MiB).
DEFAULT_ZIP_MAX_UNCOMPRESSED = 200 * 1024 * 1024
# Подозрительное количество записей в одном архиве.
DEFAULT_ZIP_MAX_ENTRIES = 10_000

# Имя файла, когда исходное невозможно восстановить.
FALLBACK_FILENAME = "unnamed"
# Ограничение длины имени (в колонке String(512) оставим запас на путь).
MAX_FILENAME_LENGTH = 255

# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------

# Управляющие символы (включая NUL) и недопустимые в именах символы Windows.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_CHARS_RE = re.compile(r'[\\/:*?"<>|]')
# Имена, зарезервированные Windows — не должны становиться объектами.
_WINDOWS_RESERVED_RE = re.compile(
    r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE
)


def sanitize_path(filename: str | None) -> str:
    """Возвращает безопасное базовое имя файла.

    Защита от path traversal: удаляются любые компоненты каталогов
    (``../``, абсолютные пути, обратные слеши), управляющие символы
    и запрещённые символы. Пустой/мусорный вход даёт ``FALLBACK_FILENAME``.
    """
    if not filename:
        return FALLBACK_FILENAME
    name = _CONTROL_CHARS_RE.sub("", str(filename))
    # Нормализуем разделители и берём последний компонент — это базовое имя.
    name = name.replace("\\", "/")
    name = name.rsplit("/", 1)[-1]
    # Запрещённые символы → подчёркивания (имя остаётся читаемым).
    name = _UNSAFE_CHARS_RE.sub("_", name)
    name = name.strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if name in ("", ".", "..") or _WINDOWS_RESERVED_RE.match(name):
        return FALLBACK_FILENAME
    if len(name) > MAX_FILENAME_LENGTH:
        # Обрезаем по базе имени, сохраняя расширение.
        base, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 32:
            keep = MAX_FILENAME_LENGTH - len(ext) - 1
            name = f"{base[:keep]}.{ext}"
        else:
            name = name[:MAX_FILENAME_LENGTH]
    return name


# ---------------------------------------------------------------------------
# Magic bytes / MIME
# ---------------------------------------------------------------------------

# Сигнатуры: (смещение, префикс) → MIME. Проверяются по порядку, первые
# выигрывают (RIFF/WEBP проверяется раньше общего RIFF, если появится).
MAGIC_SIGNATURES: tuple[tuple[bytes, int, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", 0, "image/png"),
    (b"\xff\xd8\xff", 0, "image/jpeg"),
    (b"GIF87a", 0, "image/gif"),
    (b"GIF89a", 0, "image/gif"),
    (b"%PDF", 0, "application/pdf"),
    (b"PK\x03\x04", 0, "application/zip"),
    (b"PK\x05\x06", 0, "application/zip"),
    (b"PK\x07\x08", 0, "application/zip"),
    (b"\x1f\x8b", 0, "application/gzip"),
    (b"7z\xbc\xaf\x27\x1c", 0, "application/x-7z-compressed"),
    (b"Rar!\x1a\x07", 0, "application/vnd.rar"),
    (b"BM", 0, "image/bmp"),
    (b"RIFF", 0, "application/octet-stream"),  # заглушка: см. WEBP ниже
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", 0, "application/x-ole-storage"),
    (b"MZ", 0, "application/x-dosexec"),
    (b"\x7fELF", 0, "application/x-executable"),
    (b"ustar", 257, "application/x-tar"),
)

# Форматы внутри контейнера RIFF: префикс с 8 по 12 байт.
_RIFF_SUBTYPES: tuple[tuple[bytes, str], ...] = (
    (b"WEBP", "image/webp"),
    (b"AVI ", "video/avi"),
    (b"WAVE", "audio/wav"),
)

# OOXML (docx/xlsx/pptx) и ODF — это ZIP-контейнеры: magic укажет
# application/zip, и это совместимо с заявленным офисным MIME.
_ZIP_CONTAINER_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
    "application/java-archive",
    "application/apk",
    "application/vnd.android.package-archive",
    "application/epub+zip",
}

# Заявленный MIME, который не обязан подтверждаться сигнатурой: для них
# любое содержимое допустимо (детектор ничего не «опровергает»).
_TOLERANT_MIMES = {
    "application/octet-stream",
    "text/plain",
    "text/csv",
    "application/json",
    "application/x-empty",
    "message/rfc822",
}


def _normalize_mime(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).split(";", 1)[0].strip().lower() or None


def detect_magic_mime(file_bytes: bytes) -> str | None:
    """Определяет MIME по magic bytes; None — сигнатура неизвестна."""
    if not file_bytes:
        return None
    for signature, offset, mime in MAGIC_SIGNATURES:
        if file_bytes.startswith(signature, offset):
            if mime == "application/octet-stream" and signature == b"RIFF":
                # RIFF — контейнер: уточняем подтип по байтам 8..12.
                for sub_prefix, sub_mime in _RIFF_SUBTYPES:
                    if file_bytes.startswith(sub_prefix, 8):
                        return sub_mime
                return None
            return mime
    return None


def validate_magic_bytes(file_bytes: bytes, expected_mime: str | None) -> bool:
    """Проверяет, что содержимое не противоречит заявленному MIME-типу.

    True — противоречия нет (сигнатура совпала, либо детектор не знает
    формат/заявленный тип толерантен). False — файл выглядит как другой
    формат, чем заявлено (например, ZIP, подписанный как image/png).
    """
    declared = _normalize_mime(expected_mime)
    if declared is None or declared in _TOLERANT_MIMES:
        return True
    detected = detect_magic_mime(file_bytes)
    if detected is None:
        # Неизвестная сигнатура: опровергнуть заявленный тип нельзя.
        return True
    if detected == declared:
        return True
    # OOXML/ODF/jar/epub живут внутри ZIP-контейнеров.
    zip_container = detected == "application/zip" and declared in _ZIP_CONTAINER_MIMES
    # Legacy Office (.doc/.xls/.ppt) — OLE2-контейнер.
    legacy_office = (
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
    )
    ole_container = detected == "application/x-ole-storage" and declared in legacy_office
    return zip_container or ole_container


# ---------------------------------------------------------------------------
# ZIP bomb
# ---------------------------------------------------------------------------

# Magic-префиксы семейств ZIP (локальная запись, пустой архив, spanned).
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


@dataclass(frozen=True)
class ZipBombResult:
    """Итог проверки архива на ZIP bomb."""

    safe: bool
    is_zip: bool
    entries: int
    compressed_size: int
    uncompressed_size: int
    ratio: float
    reason: str | None = None


def _skip_result(file_bytes: bytes) -> ZipBombResult:
    return ZipBombResult(
        safe=True,
        is_zip=False,
        entries=0,
        compressed_size=len(file_bytes),
        uncompressed_size=len(file_bytes),
        ratio=1.0,
    )


def check_zip_bomb(
    file_bytes: bytes,
    max_ratio: float = DEFAULT_ZIP_MAX_RATIO,
    max_size: int = DEFAULT_ZIP_MAX_UNCOMPRESSED,
) -> ZipBombResult:
    """Проверяет ZIP-архив на признаки «бомбы».

    Методы локальные (без распаковки): declared-размеры записей из
    центрального каталога, соотношение распакованный/упакованный размер,
    ограничение на число записей. Невалидный ZIP с magic-префиксом тоже
    помечается небезопасным — битые заголовки частый приём обхода проверок.

    ``max_ratio`` — максимальное сжатие, ``max_size`` — максимальный
    суммарный декларируемый распакованный размер в байтах.
    """
    if not file_bytes or not file_bytes.startswith(_ZIP_MAGICS):
        return _skip_result(file_bytes)

    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, NotImplementedError, OSError, ValueError) as exc:
        return ZipBombResult(
            safe=False,
            is_zip=True,
            entries=0,
            compressed_size=len(file_bytes),
            uncompressed_size=0,
            ratio=0.0,
            reason=f"Не удалось прочитать ZIP-архив: {exc}",
        )

    total_uncompressed = sum(item.file_size for item in infos)
    compressed = max(len(file_bytes), 1)
    ratio = total_uncompressed / compressed

    if len(infos) > DEFAULT_ZIP_MAX_ENTRIES:
        reason = (
            f"Слишком много записей в архиве: {len(infos)} "
            f"(максимум {DEFAULT_ZIP_MAX_ENTRIES})"
        )
    elif total_uncompressed > max_size:
        # Сюда же попадают отдельные записи больше лимита: их размер входит
        # в суммарный распакованный размер.
        reason = (
            f"Распакованный размер архива {total_uncompressed} байт превышает "
            f"лимит {max_size}"
        )
    elif ratio > max_ratio:
        reason = (
            f"Подозрительная степень сжатия архива: {ratio:.1f}x "
            f"(максимум {max_ratio:.0f}x)"
        )
    else:
        reason = None

    return ZipBombResult(
        safe=reason is None,
        is_zip=True,
        entries=len(infos),
        compressed_size=len(file_bytes),
        uncompressed_size=total_uncompressed,
        ratio=ratio,
        reason=reason,
    )


__all__ = [
    "DEFAULT_ZIP_MAX_ENTRIES",
    "DEFAULT_ZIP_MAX_RATIO",
    "DEFAULT_ZIP_MAX_UNCOMPRESSED",
    "FALLBACK_FILENAME",
    "MAGIC_SIGNATURES",
    "MAX_FILENAME_LENGTH",
    "ZipBombResult",
    "check_zip_bomb",
    "detect_magic_mime",
    "sanitize_path",
    "validate_magic_bytes",
]
