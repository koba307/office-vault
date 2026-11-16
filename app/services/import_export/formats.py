"""Разбор файлов импорта: определение формата, кодировки и чтение таблиц.

Модуль намеренно не зависит от БД и внешних тяжёлых библиотек: XLSX читается
через ``zipfile`` + ``xml.etree``, XLS — минимальным BIFF8-ридером (OLE2).
Все парсеры возвращают :class:`ParsedTable` с заголовками и строками-словарями,
чтобы предпросмотр и импорт работали с единой структурой.
"""

import codecs
import csv
import io
import json
import math
import re
import struct
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from xml.etree import ElementTree

FORMAT_XLSX = "XLSX"
FORMAT_XLS = "XLS"
FORMAT_CSV = "CSV"
FORMAT_TSV = "TSV"
FORMAT_JSON = "JSON"
FORMAT_JSONL = "JSONL"
FORMAT_XML = "XML"
FORMAT_TXT = "TXT"

SUPPORTED_FORMATS = (
    FORMAT_XLSX,
    FORMAT_XLS,
    FORMAT_CSV,
    FORMAT_TSV,
    FORMAT_JSON,
    FORMAT_JSONL,
    FORMAT_XML,
    FORMAT_TXT,
)

# Табличные форматы, где первая строка считается заголовком.
TABULAR_FORMATS = (FORMAT_XLSX, FORMAT_XLS, FORMAT_CSV, FORMAT_TSV, FORMAT_TXT)

_EXTENSION_FORMATS = {
    ".xlsx": FORMAT_XLSX,
    ".xlsm": FORMAT_XLSX,
    ".xls": FORMAT_XLS,
    ".csv": FORMAT_CSV,
    ".tsv": FORMAT_TSV,
    ".json": FORMAT_JSON,
    ".jsonl": FORMAT_JSONL,
    ".ndjson": FORMAT_JSONL,
    ".xml": FORMAT_XML,
    ".txt": FORMAT_TXT,
}

_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_END_OF_CHAIN = 0xFFFFFFFE
_FREE_SECTOR = 0xFFFFFFFF

_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

_MAX_HEADER_LENGTH = 128
_CSV_DELIMITERS = (",", ";", "\t", "|")


class FormatError(ValueError):
    """Базовая ошибка определения или разбора формата."""


class UnsupportedFormatError(FormatError):
    """Формат файла не поддерживается импортом."""


class FormatParseError(FormatError):
    """Файл повреждён или не соответствует заявленному формату."""


@dataclass
class ParsedTable:
    """Результат разбора: заголовки, строки и метаданные источника."""

    headers: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)
    format: str = FORMAT_TXT
    encoding: str | None = None

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def column_values(self, header: str) -> list[str]:
        return [row.get(header, "") for row in self.rows]


# ---------------------------------------------------------------------------
# Определение кодировки
# ---------------------------------------------------------------------------


def _detect_bom_encoding(file_bytes: bytes) -> str | None:
    for bom, encoding in _BOMS:
        if file_bytes.startswith(bom):
            return encoding
    return None


def _looks_like_utf16_without_bom(file_bytes: bytes) -> str | None:
    sample = file_bytes[: min(len(file_bytes), 512)]
    if len(sample) < 4:
        return None
    even_nuls = sample[0::2].count(0)
    odd_nuls = sample[1::2].count(0)
    half = max(1, len(sample) // 2)
    if odd_nuls / half > 0.3 and even_nuls / half < 0.05:
        return "utf-16-le"
    if even_nuls / half > 0.3 and odd_nuls / half < 0.05:
        return "utf-16-be"
    return None


def _text_quality(text: str) -> float:
    """Оценка «осмысленности» декодирования: буквы и кириллица важнее мусора."""
    if not text:
        return 0.0
    letters = sum(1 for ch in text if ch.isalpha())
    cyrillic = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\r\n\t")
    return (letters + cyrillic * 0.5 - controls * 4) / len(text)


def detect_encoding(file_bytes: bytes) -> str:
    """Возвращает наиболее вероятную кодировку текста.

    Сначала проверяются BOM и UTF-16 без BOM, затем строгий UTF-8, затем
    необязательный ``chardet`` (если установлен) и эвристика для
    однобайтовых кириллических кодировок.
    """
    if not file_bytes:
        return "utf-8"

    bom_encoding = _detect_bom_encoding(file_bytes)
    if bom_encoding:
        return bom_encoding

    utf16 = _looks_like_utf16_without_bom(file_bytes)
    if utf16:
        return utf16

    try:
        file_bytes.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass

    chardet_encoding = _chardet_encoding(file_bytes)
    if chardet_encoding:
        return chardet_encoding

    candidates = ("utf-8", "cp1251", "cp1252", "koi8-r")
    best_encoding = "cp1251"
    best_score = float("-inf")
    for encoding in candidates:
        try:
            text = file_bytes.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        score = _text_quality(text)
        # Небольшой приоритет кириллице: проект русскоязычный.
        if encoding == "cp1251" and any("а" <= ch.lower() <= "я" for ch in text):
            score += 0.05
        if score > best_score:
            best_score = score
            best_encoding = encoding
    return best_encoding


def _chardet_encoding(file_bytes: bytes) -> str | None:
    try:  # pragma: no cover - зависит от окружения
        import chardet  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - типичный случай
        return None
    try:
        result = chardet.detect(file_bytes)
    except Exception:  # noqa: BLE001 - сторонняя библиотека не должна ломать импорт
        return None
    encoding = (result or {}).get("encoding")
    confidence = float((result or {}).get("confidence") or 0)
    if not encoding or confidence < 0.6:
        return None
    normalized = str(encoding).lower()
    if normalized in {"ascii", "utf-8", "utf8"}:
        return "utf-8"
    return normalized


def decode_bytes(file_bytes: bytes, encoding: str | None = None) -> str:
    """Декодирует байты, мягко заменяя некорректные символы."""
    chosen = encoding or detect_encoding(file_bytes)
    try:
        return file_bytes.decode(chosen)
    except (UnicodeDecodeError, LookupError):
        try:
            return file_bytes.decode(chosen, errors="replace")
        except LookupError:
            return file_bytes.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Определение формата
# ---------------------------------------------------------------------------


def _extension(filename: str | None) -> str:
    name = (filename or "").strip().lower()
    dot = name.rfind(".")
    return name[dot:] if dot >= 0 else ""


def _text_format(file_bytes: bytes) -> str | None:
    sample = decode_bytes(file_bytes).lstrip("\ufeff \t\r\n")
    if not sample:
        return None

    if sample.startswith(("{", "[")):
        try:
            json.loads(sample)
            return FORMAT_JSON
        except json.JSONDecodeError:
            if _looks_like_jsonl(sample):
                return FORMAT_JSONL
    if _looks_like_jsonl(sample):
        return FORMAT_JSONL
    if sample.startswith("<"):
        return FORMAT_XML

    delimiter = _detect_delimiter(sample)
    if delimiter == "\t":
        return FORMAT_TSV
    if delimiter in {",", ";"}:
        return FORMAT_CSV
    return None


def _looks_like_jsonl(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    for line in lines[:20]:
        if not line.startswith("{"):
            return False
        try:
            json.loads(line)
        except json.JSONDecodeError:
            return False
    return True


def _detect_delimiter(text: str) -> str | None:
    lines = [line for line in text.splitlines() if line.strip()][:20]
    if not lines:
        return None
    best: str | None = None
    best_score = 0.0
    for delimiter in _CSV_DELIMITERS:
        counts = [line.count(delimiter) for line in lines]
        non_zero = [count for count in counts if count > 0]
        if not non_zero:
            continue
        consistency = len(non_zero) / len(lines)
        score = consistency * (sum(non_zero) / len(non_zero))
        if consistency >= 0.6 and score > best_score:
            best = delimiter
            best_score = score
    return best


def detect_format(file_bytes: bytes, filename: str | None = None) -> str:
    """Определяет формат файла по содержимому, расширение — уточняющий признак."""
    if not file_bytes:
        raise FormatParseError("Файл пуст.")

    extension_format = _EXTENSION_FORMATS.get(_extension(filename))

    if file_bytes.startswith(_ZIP_MAGIC):
        if _zip_has_xlsx(file_bytes):
            return FORMAT_XLSX
        return extension_format or FORMAT_XLSX

    if file_bytes.startswith(_OLE_MAGIC):
        return FORMAT_XLS

    text_format = _text_format(file_bytes)
    if text_format:
        return text_format

    if extension_format:
        return extension_format
    return FORMAT_TXT


def _zip_has_xlsx(file_bytes: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return False
    return "xl/workbook.xml" in names or "xl/workbook.bin" in names


# ---------------------------------------------------------------------------
# Табличные примитивы
# ---------------------------------------------------------------------------


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _header_labels(matrix: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """Берёт первую непустую строку как заголовок и убирает повторы имён."""
    data = [row for row in matrix if any(cell.strip() for cell in row)]
    if not data:
        return [], []
    raw_headers = data[0]
    headers: list[str] = []
    seen: Counter[str] = Counter()
    for index, raw in enumerate(raw_headers):
        label = _cell_text(raw)[:_MAX_HEADER_LENGTH] or f"column_{index + 1}"
        seen[label] += 1
        if seen[label] > 1:
            label = f"{label}_{seen[label]}"
        headers.append(label)
    return headers, data[1:]


def _table_from_matrix(
    matrix: list[list[str]],
    *,
    fmt: str,
    encoding: str | None = None,
    headers: list[str] | None = None,
) -> ParsedTable:
    if headers is None:
        headers, data_rows = _header_labels(matrix)
    else:
        headers = list(headers)
        data_rows = [row for row in matrix if any(_cell_text(cell) for cell in row)]
    rows: list[dict[str, str]] = []
    for raw_row in data_rows:
        row: dict[str, str] = {}
        for index, header in enumerate(headers):
            value = raw_row[index] if index < len(raw_row) else ""
            row[header] = _cell_text(value)
        if any(row.values()):
            rows.append(row)
    return ParsedTable(headers=headers, rows=rows, format=fmt, encoding=encoding)


def _table_from_dicts(
    records: list[dict[str, object]], *, fmt: str, encoding: str | None = None
) -> ParsedTable:
    headers: list[str] = []
    for record in records:
        for key in record:
            if key not in headers:
                headers.append(key)
    rows = [{header: _cell_text(record.get(header)) for header in headers} for record in records]
    return ParsedTable(
        headers=headers,
        rows=[row for row in rows if any(row.values())],
        format=fmt,
        encoding=encoding,
    )


# ---------------------------------------------------------------------------
# CSV / TSV / TXT
# ---------------------------------------------------------------------------


def _csv_matrix(text: str, delimiter: str | None) -> list[list[str]]:
    if delimiter:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    else:
        sample = text[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = None
        if dialect is not None:
            reader = csv.reader(io.StringIO(text), dialect)
        else:
            reader = csv.reader(io.StringIO(text), delimiter=_detect_delimiter(sample) or ",")
    return [[_cell_text(cell) for cell in row] for row in reader]


def parse_csv(file_bytes: bytes, encoding: str | None = None) -> ParsedTable:
    """Разбирает CSV; разделитель определяется автоматически (запятая/точка с запятой)."""
    chosen = encoding or detect_encoding(file_bytes)
    text = decode_bytes(file_bytes, chosen)
    return _table_from_matrix(_csv_matrix(text, None), fmt=FORMAT_CSV, encoding=chosen)


def parse_tsv(file_bytes: bytes, encoding: str | None = None) -> ParsedTable:
    """Разбирает TSV (табуляция как разделитель)."""
    chosen = encoding or detect_encoding(file_bytes)
    text = decode_bytes(file_bytes, chosen)
    return _table_from_matrix(_csv_matrix(text, "\t"), fmt=FORMAT_TSV, encoding=chosen)


def parse_txt(file_bytes: bytes, encoding: str | None = None) -> ParsedTable:
    """Разбирает TXT: пытается найти разделитель, иначе читает построчно."""
    chosen = encoding or detect_encoding(file_bytes)
    text = decode_bytes(file_bytes, chosen)
    delimiter = _detect_delimiter(text)
    if delimiter:
        return _table_from_matrix(_csv_matrix(text, delimiter), fmt=FORMAT_TXT, encoding=chosen)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ParsedTable(format=FORMAT_TXT, encoding=chosen)
    matrix = [[line] for line in lines]
    return _table_from_matrix(matrix, fmt=FORMAT_TXT, encoding=chosen, headers=["text"])


# ---------------------------------------------------------------------------
# JSON / JSONL
# ---------------------------------------------------------------------------


def _json_records(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("records", "items", "rows", "data", "result"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
            if isinstance(nested, dict) and key == "data":
                return [nested]
        return [payload]
    raise FormatParseError("JSON должен содержать объект или массив объектов.")


def parse_json(file_bytes: bytes, encoding: str | None = None) -> ParsedTable:
    """Разбирает JSON-массив объектов или объект с обёрткой ``records``/``items``."""
    chosen = encoding or detect_encoding(file_bytes)
    text = decode_bytes(file_bytes, chosen)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FormatParseError(f"Некорректный JSON: {exc.msg}.") from exc
    return _table_from_dicts(_json_records(payload), fmt=FORMAT_JSON, encoding=chosen)


def parse_jsonl(file_bytes: bytes, encoding: str | None = None) -> ParsedTable:
    """Разбирает JSONL/NDJSON: по одному объекту на строку."""
    chosen = encoding or detect_encoding(file_bytes)
    text = decode_bytes(file_bytes, chosen)
    records: list[dict[str, object]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise FormatParseError(f"Некорректный JSON в строке {number}: {exc.msg}.") from exc
        if not isinstance(payload, dict):
            raise FormatParseError(f"Строка {number} JSONL должна быть объектом.")
        records.append(payload)
    return _table_from_dicts(records, fmt=FORMAT_JSONL, encoding=chosen)


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------


def _local_name(tag: object) -> str:
    text = str(tag)
    return text.rsplit("}", 1)[-1] if "}" in text else text


# Корневые теги, которые трактуются как контейнер строк, а не как одна запись.
_XML_CONTAINERS = frozenset(
    {"records", "items", "rows", "list", "data", "entries", "table", "root", "dataset"}
)


def _xml_value(element: ElementTree.Element) -> str:
    if len(element):
        return " ".join(part.strip() for part in element.itertext() if part.strip())
    return (element.text or "").strip()


def _xml_row(element: ElementTree.Element) -> dict[str, str]:
    row: dict[str, str] = {key: _cell_text(value) for key, value in element.attrib.items()}
    children = [child for child in element if isinstance(child.tag, str)]
    if children:
        for child in children:
            key = _local_name(child.tag)
            value = _xml_value(child)
            if key in row and value:
                row[key] = f"{row[key]}; {value}" if row[key] else value
            else:
                row[key] = value
    elif (element.text or "").strip():
        row["value"] = element.text.strip()
    return {key: value for key, value in row.items() if value}


def parse_xml(file_bytes: bytes, encoding: str | None = None) -> ParsedTable:
    """Разбирает XML: повторяющиеся элементы становятся строками, атрибуты — колонками."""
    chosen = encoding or detect_encoding(file_bytes)
    text = decode_bytes(file_bytes, chosen)
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise FormatParseError(f"Некорректный XML: {exc}.") from exc

    children = [child for child in root if isinstance(child.tag, str)]
    if not children:
        row = _xml_row(root)
        return _table_from_dicts([row] if row else [], fmt=FORMAT_XML, encoding=chosen)

    counts = Counter(_local_name(child.tag) for child in children)
    tag, count = counts.most_common(1)[0]
    if count > 1:
        candidates = [child for child in children if _local_name(child.tag) == tag]
    elif _local_name(root.tag).lower() in _XML_CONTAINERS:
        candidates = children
    elif len(children) == 1 and len(children[0]):
        candidates = [children[0]]
    else:
        candidates = [root]
    rows = [row for row in (_xml_row(element) for element in candidates) if row]
    return _table_from_dicts(rows, fmt=FORMAT_XML, encoding=chosen)


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------


def _column_index(reference: str) -> int:
    letters = re.match(r"([A-Z]+)", reference.upper())
    if not letters:
        return 0
    index = 0
    for char in letters.group(1):
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _xlsx_shared_strings(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    values: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) != "si":
            continue
        parts = [node.text or "" for node in element.iter() if _local_name(node.tag) == "t"]
        values.append("".join(parts))
    return values


def _xlsx_first_sheet(archive: zipfile.ZipFile, names: set[str]) -> str:
    sheet_path: str | None = None
    try:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relations: dict[str, str] = {}
        if "xl/_rels/workbook.xml.rels" in names:
            rels_root = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            for relation in rels_root:
                relation_id = relation.get("Id")
                target = relation.get("Target")
                if relation_id and target:
                    relations[relation_id] = target
        first_sheet = next(
            (element for element in workbook.iter() if _local_name(element.tag) == "sheet"),
            None,
        )
        if first_sheet is not None:
            relation_id = next(
                (value for key, value in first_sheet.attrib.items() if _local_name(key) == "id"),
                None,
            )
            target = relations.get(relation_id or "")
            if target:
                target = target.replace("\\", "/")
                if target.startswith("/"):
                    sheet_path = target.lstrip("/")
                elif target.startswith("xl/"):
                    sheet_path = target
                else:
                    sheet_path = f"xl/{target}"
    except (KeyError, ElementTree.ParseError):
        sheet_path = None

    if sheet_path and sheet_path in names:
        return sheet_path
    candidates = sorted(name for name in names if name.startswith("xl/worksheets/sheet"))
    if not candidates:
        raise FormatParseError("В XLSX не найден лист с данными.")
    return candidates[0]


def _xlsx_cell_text(cell: ElementTree.Element, shared: list[str]) -> str:
    cell_type = cell.get("t") or "n"
    value_node = None
    inline_node = None
    for child in cell:
        name = _local_name(child.tag)
        if name == "v":
            value_node = child
        elif name == "is":
            inline_node = child
    raw = (value_node.text or "") if value_node is not None else ""
    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if cell_type == "inlineStr" and inline_node is not None:
        return "".join(
            node.text or "" for node in inline_node.iter() if _local_name(node.tag) == "t"
        )
    if cell_type == "b":
        return "true" if raw.strip() in {"1", "true", "TRUE"} else "false"
    return _cell_text(raw)


def _xlsx_matrix(sheet_bytes: bytes, shared: list[str]) -> list[list[str]]:
    root = ElementTree.fromstring(sheet_bytes)
    matrix: list[list[str]] = []
    for row_element in root.iter():
        if _local_name(row_element.tag) != "row":
            continue
        values: dict[int, str] = {}
        next_index = 0
        for cell in row_element:
            if _local_name(cell.tag) != "c":
                continue
            reference = cell.get("r")
            index = _column_index(reference) if reference else next_index
            values[index] = _xlsx_cell_text(cell, shared)
            next_index = index + 1
        if not values:
            continue
        width = max(values) + 1
        matrix.append([values.get(index, "") for index in range(width)])
    return matrix


def parse_xlsx(file_bytes: bytes) -> ParsedTable:
    """Разбирает первый лист XLSX без внешних библиотек."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            names = set(archive.namelist())
            shared = _xlsx_shared_strings(archive, names)
            sheet_path = _xlsx_first_sheet(archive, names)
            sheet_bytes = archive.read(sheet_path)
    except zipfile.BadZipFile as exc:
        raise FormatParseError("Файл не является корректным XLSX.") from exc
    except KeyError as exc:
        raise FormatParseError("В XLSX отсутствуют обязательные части книги.") from exc
    try:
        matrix = _xlsx_matrix(sheet_bytes, shared)
    except ElementTree.ParseError as exc:
        raise FormatParseError("Повреждён XML листа XLSX.") from exc
    return _table_from_matrix(matrix, fmt=FORMAT_XLSX, encoding="utf-8")


# ---------------------------------------------------------------------------
# XLS (BIFF8 внутри OLE2)
# ---------------------------------------------------------------------------


def _sector(data: bytes, index: int, sector_size: int) -> bytes:
    start = (index + 1) * sector_size
    if start >= len(data):
        raise FormatParseError("Сектор OLE2 вне файла.")
    return data[start : start + sector_size]


def _read_chain(data: bytes, start: int, fat: list[int], sector_size: int) -> bytes:
    chunks: list[bytes] = []
    sector = start
    guard = 0
    while sector not in (_END_OF_CHAIN, _FREE_SECTOR) and guard <= len(fat):
        if sector >= len(fat):
            break
        chunks.append(_sector(data, sector, sector_size))
        sector = fat[sector]
        guard += 1
    return b"".join(chunks)


def _read_mini_chain(mini_stream: bytes, mini_fat: list[int], start: int, mini_size: int) -> bytes:
    chunks: list[bytes] = []
    sector = start
    guard = 0
    while sector not in (_END_OF_CHAIN, _FREE_SECTOR) and guard <= len(mini_fat):
        if sector >= len(mini_fat):
            break
        offset = sector * mini_size
        chunks.append(mini_stream[offset : offset + mini_size])
        sector = mini_fat[sector]
        guard += 1
    return b"".join(chunks)


def _ole_stream(file_bytes: bytes) -> bytes:
    if len(file_bytes) < 512 or not file_bytes.startswith(_OLE_MAGIC):
        raise FormatParseError("Файл не является OLE2-документом (XLS).")
    sector_shift = int.from_bytes(file_bytes[0x1E:0x20], "little")
    if sector_shift not in (9, 12):
        raise FormatParseError("Неподдерживаемый размер сектора OLE2.")
    sector_size = 1 << sector_shift
    mini_shift = int.from_bytes(file_bytes[0x20:0x22], "little")
    mini_size = 1 << mini_shift if mini_shift else 64
    num_fat = int.from_bytes(file_bytes[0x2C:0x30], "little")
    dir_start = int.from_bytes(file_bytes[0x30:0x34], "little")
    mini_cutoff = int.from_bytes(file_bytes[0x38:0x3C], "little") or 4096
    minifat_start = int.from_bytes(file_bytes[0x3C:0x40], "little")
    num_minifat = int.from_bytes(file_bytes[0x40:0x44], "little")
    difat_start = int.from_bytes(file_bytes[0x44:0x48], "little")
    num_difat = int.from_bytes(file_bytes[0x48:0x4C], "little")

    difat = list(struct.unpack("<109I", file_bytes[0x4C : 0x4C + 109 * 4]))
    sector = difat_start
    guard = 0
    while sector not in (_END_OF_CHAIN, _FREE_SECTOR) and guard < num_difat + 1:
        chunk = _sector(file_bytes, sector, sector_size)
        values = struct.unpack(f"<{sector_size // 4}I", chunk)
        difat.extend(values[:-1])
        sector = values[-1]
        guard += 1
    fat_sectors = [item for item in difat if item < 0xFFFFFFFA][:num_fat]
    fat: list[int] = []
    for index in fat_sectors:
        chunk = _sector(file_bytes, index, sector_size)
        fat.extend(struct.unpack(f"<{sector_size // 4}I", chunk))

    directory = _read_chain(file_bytes, dir_start, fat, sector_size)
    entries = [directory[offset : offset + 128] for offset in range(0, len(directory) - 127, 128)]
    if not entries:
        raise FormatParseError("Пустой каталог OLE2.")

    root_start = int.from_bytes(entries[0][116:120], "little")
    mini_stream = _read_chain(file_bytes, root_start, fat, sector_size)
    mini_fat: list[int] = []
    if num_minifat:
        minifat_bytes = _read_chain(file_bytes, minifat_start, fat, sector_size)
        mini_fat = list(struct.unpack(f"<{len(minifat_bytes) // 4}I", minifat_bytes))

    for entry in entries[1:]:
        name_length = int.from_bytes(entry[64:66], "little")
        if name_length < 2:
            continue
        name = entry[: name_length - 2].decode("utf-16-le", errors="ignore")
        if name not in {"Workbook", "Book"}:
            continue
        stream_start = int.from_bytes(entry[116:120], "little")
        stream_size = int.from_bytes(entry[120:124], "little")
        if stream_size >= mini_cutoff:
            stream = _read_chain(file_bytes, stream_start, fat, sector_size)
        else:
            stream = _read_mini_chain(mini_stream, mini_fat, stream_start, mini_size)
        if not stream:
            raise FormatParseError("Поток Workbook пуст.")
        return stream[:stream_size] if stream_size else stream
    raise FormatParseError("В XLS не найден поток Workbook.")


def _iter_biff(stream: bytes, start: int = 0):
    position = start
    while position + 4 <= len(stream):
        record_id, length = struct.unpack_from("<HH", stream, position)
        data = stream[position + 4 : position + 4 + length]
        yield record_id, data, position
        position += 4 + length


def _decode_rk(raw: int) -> str:
    if raw & 0x02:
        value: float = raw >> 2
        if value & 0x20000000:
            value -= 0x40000000
    else:
        bits = (raw & 0xFFFFFFFC) << 32
        value = struct.unpack("<d", struct.pack("<Q", bits))[0]
    if raw & 0x01:
        value /= 100
    return _format_number(value)


def _format_number(value: float) -> str:
    if math.isnan(value) or math.isinf(value):
        return ""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.15g}"


class _SstReader:
    """Чтение SST с переходами через CONTINUE (кириллица в XLS — UTF-16)."""

    def __init__(self, segments: list[bytes]):
        self.segments = segments
        self.segment = 0
        self.position = 0

    def ensure(self) -> bool:
        while self.segment < len(self.segments) and self.position >= len(
            self.segments[self.segment]
        ):
            self.segment += 1
            self.position = 0
        return self.segment < len(self.segments)

    def read(self, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            if not self.ensure():
                raise FormatParseError("Неожиданный конец SST.")
            current = self.segments[self.segment]
            take = min(remaining, len(current) - self.position)
            chunks.append(current[self.position : self.position + take])
            self.position += take
            remaining -= take
        return b"".join(chunks)

    def read_u8(self) -> int:
        return self.read(1)[0]

    def read_u16(self) -> int:
        return struct.unpack("<H", self.read(2))[0]

    def read_u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def at_segment_end(self) -> bool:
        return (
            self.segment < len(self.segments)
            and self.position >= len(self.segments[self.segment])
            and self.segment + 1 < len(self.segments)
        )

    def next_segment_flag(self) -> int | None:
        self.segment += 1
        self.position = 0
        if self.segment >= len(self.segments):
            return None
        return self.read_u8()


def _parse_sst(segments: list[bytes]) -> list[str]:
    if not segments:
        return []
    reader = _SstReader(segments)
    try:
        reader.read_u32()  # total strings
        unique = reader.read_u32()
    except (struct.error, FormatParseError):
        return []

    values: list[str] = []
    for _ in range(unique):
        try:
            length = reader.read_u16()
            flags = reader.read_u8()
            rich_runs = reader.read_u16() if flags & 0x08 else 0
            extended_size = reader.read_u32() if flags & 0x04 else 0
            compressed = not (flags & 0x01)

            parts: list[str] = []
            remaining = length
            while remaining > 0:
                if reader.at_segment_end():
                    continuation = reader.next_segment_flag()
                    if continuation is None:
                        break
                    compressed = not (continuation & 0x01)
                if reader.segment >= len(reader.segments):
                    break
                current = reader.segments[reader.segment]
                available = len(current) - reader.position
                if available <= 0:
                    if not reader.ensure():
                        break
                    continue
                if compressed:
                    take = min(remaining, available)
                    chunk = reader.read(take)
                    parts.append(chunk.decode("latin-1", errors="replace"))
                    remaining -= take
                else:
                    take = min(remaining, available // 2)
                    if take <= 0:
                        # Символ разорван между CONTINUE — дочитываем байты и флаг.
                        reader.position = len(current)
                        continue
                    chunk = reader.read(take * 2)
                    parts.append(chunk.decode("utf-16-le", errors="replace"))
                    remaining -= take

            if rich_runs:
                reader.read(4 * rich_runs)
            if extended_size:
                reader.read(extended_size)
            values.append("".join(parts))
        except FormatParseError:
            break
    return values


def _biff_value_text(record_id: int, data: bytes, shared: list[str]) -> tuple[int, int, str] | None:
    if len(data) < 6:
        return None
    row, column = struct.unpack_from("<HH", data, 0)
    if record_id == 0x00FD and len(data) >= 10:  # LABELSST
        index = struct.unpack_from("<I", data, 6)[0]
        return row, column, shared[index] if index < len(shared) else ""
    if record_id == 0x0204 and len(data) >= 8:  # LABEL
        length = struct.unpack_from("<H", data, 6)[0]
        flags = data[8] if len(data) > 8 else 0
        payload = data[9:]
        if flags & 0x01:
            return row, column, payload[: length * 2].decode("utf-16-le", errors="replace")
        return row, column, payload[:length].decode("latin-1", errors="replace")
    if record_id == 0x0203 and len(data) >= 14:  # NUMBER
        value = struct.unpack_from("<d", data, 6)[0]
        return row, column, _format_number(value)
    if record_id == 0x027E and len(data) >= 10:  # RK
        raw = struct.unpack_from("<I", data, 6)[0]
        return row, column, _decode_rk(raw)
    if record_id == 0x0205 and len(data) >= 8:  # BOOLERR
        kind, value = data[6], data[7]
        if kind == 0:
            return row, column, "false" if value == 0 else "true"
        return row, column, ""
    if record_id == 0x0006 and len(data) >= 14:  # FORMULA: кэшированный результат
        if data[6:8] == b"\xff\xff":
            return row, column, ""
        value = struct.unpack_from("<d", data, 6)[0]
        return row, column, _format_number(value)
    return None


def _biff_string(data: bytes) -> str:
    if len(data) < 3:
        return ""
    length = struct.unpack_from("<H", data, 0)[0]
    flags = data[2]
    payload = data[3:]
    if flags & 0x01:
        return payload[: length * 2].decode("utf-16-le", errors="replace")
    return payload[:length].decode("latin-1", errors="replace")


def _biff_mulrk(data: bytes) -> list[tuple[int, int, str]]:
    """MULRK: несколько RK-значений в одной записи строки."""
    if len(data) < 12:
        return []
    row, first_column = struct.unpack_from("<HH", data, 0)
    count = max(0, (len(data) - 6) // 6)
    values: list[tuple[int, int, str]] = []
    for index in range(count):
        offset = 4 + index * 6
        if offset + 4 > len(data):
            break
        raw = struct.unpack_from("<I", data, offset + 2)[0]
        values.append((row, first_column + index, _decode_rk(raw)))
    return values


def _parse_biff_workbook(stream: bytes) -> list[list[str]]:
    """Разбирает поток Workbook BIFF8 в матрицу первого листа."""
    shared: list[str] = []
    first_sheet: int | None = None
    sst_segments: list[bytes] | None = None

    for record_id, data, _position in _iter_biff(stream):
        if record_id == 0x00FC:  # SST
            sst_segments = [data]
        elif record_id == 0x003C and sst_segments is not None:  # CONTINUE
            sst_segments.append(data)
        elif record_id == 0x0085 and len(data) >= 8:  # BOUNDSHEET
            offset = struct.unpack_from("<I", data, 0)[0]
            sheet_type = data[5] & 0x0F
            if first_sheet is None and sheet_type == 0:
                first_sheet = offset
        elif record_id == 0x000A and sst_segments is not None and shared == []:
            shared = _parse_sst(sst_segments)
        elif record_id == 0x000A and first_sheet is not None and sst_segments is not None:
            break

    if sst_segments is not None and not shared:
        shared = _parse_sst(sst_segments)
    if first_sheet is None:
        raise FormatParseError("В XLS не найден лист.")

    cells: dict[tuple[int, int], str] = {}
    pending_string: tuple[int, int] | None = None
    for record_id, data, _position in _iter_biff(stream, first_sheet):
        if record_id == 0x000A:  # EOF листа
            break
        if record_id == 0x0207 and pending_string is not None:  # STRING после FORMULA
            cells[pending_string] = _biff_string(data)
            pending_string = None
            continue
        if record_id == 0x00BD:  # MULRK
            for row, column, value in _biff_mulrk(data):
                cells[(row, column)] = value
            continue
        parsed = _biff_value_text(record_id, data, shared)
        if parsed is None:
            continue
        row, column, value = parsed
        if (
            record_id == 0x0006
            and len(data) >= 14
            and data[6:8] == b"\xff\xff"
            and data[8] == 0  # результат-строка придёт в STRING
        ):
            pending_string = (row, column)
            continue
        cells[(row, column)] = value

    if not cells:
        return []
    max_row = max(row for row, _ in cells)
    max_column = max(column for _, column in cells)
    return [
        [cells.get((row, column), "") for column in range(max_column + 1)]
        for row in range(max_row + 1)
    ]


def parse_xls(file_bytes: bytes) -> ParsedTable:
    """Разбирает XLS (BIFF8) без ``xlrd``."""
    stream = _ole_stream(file_bytes)
    matrix = _parse_biff_workbook(stream)
    return _table_from_matrix(matrix, fmt=FORMAT_XLS, encoding=None)


# ---------------------------------------------------------------------------
# Диспетчер
# ---------------------------------------------------------------------------


def parse_file(
    file_bytes: bytes,
    filename: str | None = None,
    *,
    format_name: str | None = None,
    encoding: str | None = None,
) -> ParsedTable:
    """Определяет формат (если не задан) и разбирает файл."""
    fmt = (format_name or detect_format(file_bytes, filename)).upper()
    if fmt == FORMAT_XLSX:
        return parse_xlsx(file_bytes)
    if fmt == FORMAT_XLS:
        return parse_xls(file_bytes)
    if fmt == FORMAT_CSV:
        return parse_csv(file_bytes, encoding)
    if fmt == FORMAT_TSV:
        return parse_tsv(file_bytes, encoding)
    if fmt == FORMAT_JSON:
        return parse_json(file_bytes, encoding)
    if fmt == FORMAT_JSONL:
        return parse_jsonl(file_bytes, encoding)
    if fmt == FORMAT_XML:
        return parse_xml(file_bytes, encoding)
    if fmt == FORMAT_TXT:
        return parse_txt(file_bytes, encoding)
    raise UnsupportedFormatError(f"Формат {fmt} не поддерживается.")
