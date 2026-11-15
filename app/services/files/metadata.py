"""Метаданные файлов: SHA-256, MIME по magic bytes, имена и лимиты.

Модуль не зависит от хранилища и базы данных: все проверки работают
с байтами и именем файла.
"""

import hashlib
import json
import re
import unicodedata

GENERIC_MIME_TYPE = "application/octet-stream"
DEFAULT_MAX_FILENAME_LENGTH = 255

# Расширения, которые нельзя принимать в корпоративном хранилище:
# исполняемые файлы, скрипты, макросы и установщики.
BLOCKED_EXTENSIONS = frozenset(
    {
        "apk",
        "app",
        "asp",
        "aspx",
        "bash",
        "bat",
        "cgi",
        "cmd",
        "com",
        "cpl",
        "deb",
        "dll",
        "docm",
        "dotm",
        "exe",
        "hta",
        "img",
        "inf",
        "iso",
        "jar",
        "js",
        "jse",
        "jsp",
        "lnk",
        "msi",
        "msp",
        "phtml",
        "php",
        "pif",
        "ps1",
        "psm1",
        "py",
        "rb",
        "reg",
        "rpm",
        "scf",
        "scr",
        "sh",
        "sys",
        "vbe",
        "vbs",
        "vhd",
        "vhdx",
        "wsf",
        "wsh",
        "xlsm",
        "xlam",
        "zsh",
    }
)

# Канонические MIME-типы и допустимые расширения.
MIME_EXTENSIONS: dict[str, frozenset[str]] = {
    "application/pdf": frozenset({"pdf"}),
    "application/postscript": frozenset({"ps", "eps"}),
    "application/rtf": frozenset({"rtf"}),
    "application/msword": frozenset({"doc", "dot"}),
    "application/vnd.ms-excel": frozenset({"xls", "xlt"}),
    "application/vnd.ms-powerpoint": frozenset({"ppt", "pps"}),
    "application/vnd.ms-outlook": frozenset({"msg"}),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": frozenset(
        {"docx"}
    ),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": frozenset({"xlsx"}),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": frozenset(
        {"pptx"}
    ),
    "application/vnd.oasis.opendocument.text": frozenset({"odt"}),
    "application/vnd.oasis.opendocument.spreadsheet": frozenset({"ods"}),
    "application/vnd.oasis.opendocument.presentation": frozenset({"odp"}),
    "application/epub+zip": frozenset({"epub"}),
    "application/zip": frozenset(
        {
            "zip",
            "docx",
            "xlsx",
            "pptx",
            "odt",
            "ods",
            "odp",
            "epub",
        }
    ),
    "application/gzip": frozenset({"gz", "gzip", "tgz"}),
    "application/x-bzip2": frozenset({"bz2"}),
    "application/x-xz": frozenset({"xz"}),
    "application/x-7z-compressed": frozenset({"7z"}),
    "application/vnd.rar": frozenset({"rar"}),
    "application/x-tar": frozenset({"tar"}),
    "application/x-ole-storage": frozenset({"doc", "xls", "ppt", "msg", "vsd"}),
    "application/vnd.sqlite3": frozenset({"sqlite", "sqlite3", "db"}),
    "application/json": frozenset({"json"}),
    "application/x-ndjson": frozenset({"jsonl", "ndjson"}),
    "application/xml": frozenset({"xml", "xsd", "xsl", "xslt"}),
    "application/yaml": frozenset({"yaml", "yml"}),
    "image/png": frozenset({"png"}),
    "image/jpeg": frozenset({"jpg", "jpeg", "jpe"}),
    "image/gif": frozenset({"gif"}),
    "image/webp": frozenset({"webp"}),
    "image/bmp": frozenset({"bmp"}),
    "image/tiff": frozenset({"tif", "tiff"}),
    "image/svg+xml": frozenset({"svg"}),
    "audio/mpeg": frozenset({"mp3"}),
    "audio/ogg": frozenset({"ogg", "oga"}),
    "audio/wav": frozenset({"wav"}),
    "audio/flac": frozenset({"flac"}),
    "audio/midi": frozenset({"mid", "midi"}),
    "video/mp4": frozenset({"mp4", "m4v"}),
    "video/webm": frozenset({"webm"}),
    "video/x-matroska": frozenset({"mkv"}),
    "video/quicktime": frozenset({"mov"}),
    "video/x-msvideo": frozenset({"avi"}),
    "text/plain": frozenset({"txt", "text", "log", "ini", "cfg", "conf", "rst", "nfo"}),
    "text/csv": frozenset({"csv"}),
    "text/tab-separated-values": frozenset({"tsv"}),
    "text/html": frozenset({"html", "htm"}),
    "text/markdown": frozenset({"md", "markdown"}),
}

# MIME-типы документов Office, которые физически являются ZIP-контейнером.
ZIP_CONTAINER_MIME_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation",
        "application/epub+zip",
        "application/zip",
    }
)

# Старый формат Office (OLE2).
OLE_MIME_TYPES = frozenset(
    {
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.ms-outlook",
        "application/x-ole-storage",
    }
)

_MIME_ALIASES = {
    "application/x-zip-compressed": "application/zip",
    "application/x-pdf": "application/pdf",
    "application/x-rar-compressed": "application/vnd.rar",
    "application/x-7z": "application/x-7z-compressed",
    "application/x-sqlite3": "application/vnd.sqlite3",
    "application/x-yaml": "application/yaml",
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "audio/mp3": "audio/mpeg",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/x-flac": "audio/flac",
    "video/x-m4v": "video/mp4",
    "text/xml": "application/xml",
    "text/x-yaml": "application/yaml",
    "text/x-markdown": "text/markdown",
}

_ZIP_EXTENSION_MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "epub": "application/epub+zip",
    "jar": "application/java-archive",
    "apk": "application/vnd.android.package-archive",
}

_OLE_EXTENSION_MIME = {
    "doc": "application/msword",
    "dot": "application/msword",
    "xls": "application/vnd.ms-excel",
    "xlt": "application/vnd.ms-excel",
    "ppt": "application/vnd.ms-powerpoint",
    "pps": "application/vnd.ms-powerpoint",
    "msg": "application/vnd.ms-outlook",
}

_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)

_RESERVED_WINDOWS_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_CHARS_RE = re.compile(r"[^\w\s.\-()\[\]]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)


def calculate_sha256(file_bytes: bytes) -> str:
    """Возвращает SHA-256 содержимого в hex-виде."""
    digest = hashlib.sha256()
    digest.update(bytes(file_bytes))
    return digest.hexdigest()


def normalize_mime_type(mime_type: str | None) -> str:
    """Приводит MIME к нижнему регистру без параметров и синонимов."""
    if not mime_type:
        return GENERIC_MIME_TYPE
    base = str(mime_type).split(";", 1)[0].strip().lower()
    if not base:
        return GENERIC_MIME_TYPE
    return _MIME_ALIASES.get(base, base)


def get_extension(filename: str | None) -> str | None:
    """Возвращает расширение файла без точки в нижнем регистре."""
    if not filename:
        return None
    base_name = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in base_name:
        return None
    extension = base_name.rsplit(".", 1)[1].strip().lower()
    return extension or None


def _decode_text(data: bytes) -> str | None:
    """Пытается определить текст. Возвращает None для бинарных данных."""
    if b"\x00" in data[:8192]:
        return None
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        sample = text[:8192]
        if sample:
            controls = sum(
                1 for char in sample if ord(char) < 32 and char not in "\t\r\n\f"
            )
            if controls / len(sample) > 0.05:
                return None
        return text
    return None


def _looks_like_json(text: str) -> bool:
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped or stripped[0] not in "{[" or len(stripped) > 2_000_000:
        return False
    try:
        json.loads(stripped)
    except (ValueError, TypeError):
        return False
    return True


def _text_mime_type(text: str, extension: str | None) -> str:
    stripped = text.lstrip("\ufeff \t\r\n")
    lowered = stripped[:512].lower()

    if extension == "json" or _looks_like_json(stripped):
        return "application/json"
    if extension in {"jsonl", "ndjson"}:
        return "application/x-ndjson"
    if extension == "csv":
        return "text/csv"
    if extension == "tsv":
        return "text/tab-separated-values"
    if extension == "svg" or (lowered.startswith("<svg") and "</svg>" in lowered):
        return "image/svg+xml"
    if extension in {"xml", "xsd", "xsl", "xslt"}:
        return "application/xml"
    if extension in {"html", "htm"}:
        return "text/html"
    if extension in {"md", "markdown"}:
        return "text/markdown"
    if extension in {"yaml", "yml"}:
        return "application/yaml"
    if lowered.startswith(("<!doctype html", "<html")):
        return "text/html"
    if lowered.startswith("<?xml"):
        if "<svg" in lowered:
            return "image/svg+xml"
        return "application/xml"
    return "text/plain"


def _binary_mime_type(data: bytes, extension: str | None) -> str | None:
    """Определяет формат по сигнатурам. None — сигнатура не распознана."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"%!PS-Adobe-"):
        return "application/postscript"
    if data.startswith(b"{\\rtf"):
        return "application/rtf"

    for signature, mime_type in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime_type
    if data.startswith(b"BM") and len(data) >= 14:
        return "image/bmp"

    if data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return _ZIP_EXTENSION_MIME.get(extension or "", "application/zip")
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _OLE_EXTENSION_MIME.get(extension or "", "application/x-ole-storage")
    if data.startswith(b"\x1f\x8b"):
        return "application/gzip"
    if data.startswith(b"BZh"):
        return "application/x-bzip2"
    if data.startswith(b"\xfd7zXZ\x00"):
        return "application/x-xz"
    if data.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "application/x-7z-compressed"
    if data.startswith(b"Rar!\x1a\x07"):
        return "application/vnd.rar"
    if len(data) > 262 and data[257:262] == b"ustar":
        return "application/x-tar"
    if data.startswith(b"SQLite format 3\x00"):
        return "application/vnd.sqlite3"

    if data.startswith(b"RIFF") and len(data) >= 12:
        riff_type = data[8:12]
        if riff_type == b"WAVE":
            return "audio/wav"
        if riff_type == b"WEBP":
            return "image/webp"
        if riff_type == b"AVI ":
            return "video/x-msvideo"

    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if data.startswith(b"MThd"):
        return "audio/midi"
    if data.startswith(b"ID3"):
        return "audio/mpeg"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "audio/mpeg"

    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in {b"qt  ", b"moov"}:
            return "video/quicktime"
        if brand in {b"M4A ", b"M4B ", b"M4P "}:
            return "audio/mp4"
        if brand in {b"heic", b"heix", b"mif1"}:
            return "image/heic"
        return "video/mp4"

    if data.startswith(b"\x7fELF"):
        return "application/x-elf"
    if data.startswith(b"MZ"):
        return "application/x-msdownload"
    return None


def detect_mime_type(file_bytes: bytes, filename: str | None = None) -> str:
    """Определяет MIME по magic bytes с уточнением по расширению."""
    data = bytes(file_bytes or b"")
    extension = get_extension(filename)
    if not data:
        return GENERIC_MIME_TYPE

    binary_type = _binary_mime_type(data, extension)
    if binary_type is not None:
        return binary_type

    text = _decode_text(data)
    if text is not None:
        return _text_mime_type(text, extension)
    return GENERIC_MIME_TYPE


def validate_extension(mime_type: str, filename: str | None) -> bool:
    """Проверяет, что расширение соответствует определённому MIME-типу."""
    extension = get_extension(filename)
    normalized = normalize_mime_type(mime_type)

    if extension in BLOCKED_EXTENSIONS:
        return False
    if extension is None:
        return normalized in {GENERIC_MIME_TYPE, "text/plain"}

    expected = MIME_EXTENSIONS.get(normalized)
    if expected is None:
        return True
    return extension in expected


def sanitize_filename(filename: str | None) -> str:
    """Убирает path traversal и недопустимые символы из имени файла."""
    raw = unicodedata.normalize("NFKC", str(filename or ""))
    # Windows-разделители приводим к POSIX и берём только последний сегмент.
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL_CHARS_RE.sub("", name)
    name = _UNSAFE_CHARS_RE.sub("_", name)
    name = _WHITESPACE_RE.sub(" ", name).strip().rstrip(". ")

    if not name or name in {".", ".."}:
        return "file"

    stem = name.split(".", 1)[0].upper()
    if stem in _RESERVED_WINDOWS_NAMES:
        name = f"_{name}"

    return _truncate_filename(name)


def _truncate_filename(name: str, max_length: int = DEFAULT_MAX_FILENAME_LENGTH) -> str:
    if len(name) <= max_length:
        return name
    stem, dot, suffix = name.rpartition(".")
    if dot and 0 < len(suffix) <= 16:
        head = stem[: max_length - len(suffix) - 1].rstrip(" .")
        candidate = f"{head}.{suffix}" if head else f"file.{suffix}"
        if len(candidate) <= max_length:
            return candidate
    return name[:max_length].rstrip(" .") or "file"


def check_size_limit(file_bytes: bytes, max_size: int) -> bool:
    """Проверяет, что размер файла не превышает лимит."""
    if max_size is None or max_size < 0:
        return False
    return len(file_bytes) <= max_size
