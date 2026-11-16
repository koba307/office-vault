"""Предпросмотр импорта: структура файла, авто-сопоставление полей, валидация.

Предпросмотр не пишет в БД: он только читает категорию (если передан session)
и возвращает :class:`ImportPreview`, на который опирается FSM и фоновый импорт.
"""

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Record, RecordType
from app.services.import_export.formats import (
    FORMAT_CSV,
    FORMAT_JSON,
    FORMAT_JSONL,
    FORMAT_TSV,
    FORMAT_TXT,
    FORMAT_XML,
    ParsedTable,
    decode_bytes,
    detect_encoding,
    detect_format,
    parse_file,
)
from app.services.records.fields import (
    FieldValidationError,
    normalize_field_key,
    normalize_record_type,
    required_field_definitions,
)

MAX_PREVIEW_ROWS = 5
MAX_PREVIEW_ERRORS = 20
MAX_ERRORS = 500

# Служебные колонки, которые не становятся динамическими полями.
RESERVED_COLUMNS = ("title", "type", "description", "tags")

_TITLE_ALIASES = (
    "title",
    "название",
    "наименование",
    "заголовок",
    "имя",
    "name",
    "record",
    "запись",
)
_TYPE_ALIASES = ("type", "тип", "record_type", "тип записи", "категория записи", "kind")
_DESCRIPTION_ALIASES = (
    "description",
    "описание",
    "комментарий",
    "comment",
    "notes_common",
    "примечание",
    "desc",
)
_TAGS_ALIASES = ("tags", "теги", "метки", "labels")

# Синонимы динамических полей (ключ поля → варианты заголовков).
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "login": ("login", "логин", "username", "user", "пользователь", "account", "аккаунт"),
    "password": ("password", "пароль", "pass", "pwd", "пароли"),
    "url": ("url", "ссылка", "link", "site", "сайт", "адрес", "address"),
    "notes": ("notes", "заметки", "note", "примечание"),
    "email": ("email", "e mail", "почта", "электронная почта", "mail"),
    "phone": ("phone", "телефон", "тел", "mobile", "мобильный"),
    "sip_username": ("sip username", "sip логин", "sip login", "sip user"),
    "sip_password": ("sip password", "sip пароль"),
    "server": ("server", "сервер", "host", "хост", "хост сервер"),
    "port": ("port", "порт"),
    "parameter": ("parameter", "параметр", "настройка", "setting"),
    "value": ("value", "значение", "val"),
    "config": ("config", "конфиг", "конфигурация", "configuration", "json"),
    "content": ("content", "содержание", "содержимое", "текст", "text", "instruction"),
    "source_url": ("source", "источник", "source url", "ссылка на источник"),
}

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Форматы, где кодировку имеет смысл определять (бинарные XLS/XLSX — нет).
TEXT_FORMATS = frozenset(
    {FORMAT_CSV, FORMAT_TSV, FORMAT_JSON, FORMAT_JSONL, FORMAT_XML, FORMAT_TXT}
)


class PreviewError(ValueError):
    """Базовая ошибка предпросмотра импорта."""


class PreviewFormatError(PreviewError):
    """Файл не удалось разобрать для предпросмотра."""


@dataclass(frozen=True)
class RowError:
    """Проблема одной строки импорта."""

    row: int
    field: str | None
    message: str

    def as_dict(self) -> dict[str, object]:
        return {"row": self.row, "field": self.field, "message": self.message}


@dataclass
class ValidationResult:
    """Итог проверки строк: сколько валидных, какие ошибки и дубликаты."""

    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    duplicates: int = 0
    errors: list[RowError] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


@dataclass
class ImportPreview:
    """Всё, что нужно боту для показа предпросмотра и запуска импорта."""

    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    errors: list[RowError] = field(default_factory=list)
    suggested_mapping: dict[str, str] = field(default_factory=dict)
    headers: list[str] = field(default_factory=list)
    sample_rows: list[dict[str, str]] = field(default_factory=list)
    source_format: str = ""
    encoding: str | None = None
    record_type: str = RecordType.OTHER.value
    duplicate_rows: int = 0
    errors_truncated: bool = False

    @property
    def needs_mapping(self) -> bool:
        return "title" not in self.suggested_mapping.values()


def _normalize_alias(value: str) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _alias_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for aliases, target in (
        (_TITLE_ALIASES, "title"),
        (_TYPE_ALIASES, "type"),
        (_DESCRIPTION_ALIASES, "description"),
        (_TAGS_ALIASES, "tags"),
    ):
        for alias in aliases:
            index.setdefault(_normalize_alias(alias), target)
    for target, aliases in FIELD_ALIASES.items():
        index.setdefault(_normalize_alias(target), target)
        for alias in aliases:
            index.setdefault(_normalize_alias(alias), target)
    return index


_ALIAS_INDEX = _alias_index()


def suggest_field_mapping(
    headers: list[str], record_type: RecordType | str | None = None
) -> dict[str, str]:
    """Сопоставляет колонки файла с полями записи: ``{колонка: ключ_поля}``.

    Сначала ищутся точные синонимы (рус/англ), затем частичные совпадения,
    затем колонка пробует стать динамическим полем по своему имени.
    """
    normalize_record_type(record_type or RecordType.OTHER)
    mapping: dict[str, str] = {}
    used: set[str] = set()

    normalized = {header: _normalize_alias(header) for header in headers}
    for header in headers:
        target = _ALIAS_INDEX.get(normalized[header])
        if target and target not in used:
            mapping[header] = target
            used.add(target)

    for header in headers:
        if header in mapping:
            continue
        text = normalized[header]
        if len(text) < 3:
            continue
        for alias, target in _ALIAS_INDEX.items():
            if target in used or len(alias) < 3:
                continue
            if alias in text or text in alias:
                mapping[header] = target
                used.add(target)
                break

    for header in headers:
        if header in mapping:
            continue
        try:
            key = normalize_field_key(header)
        except FieldValidationError:
            continue
        if key in used or key in RESERVED_COLUMNS:
            continue
        if not _KEY_RE.match(key):
            continue
        mapping[header] = key
        used.add(key)
    return mapping


def apply_field_mapping(
    rows: list[dict[str, str]], mapping: dict[str, str]
) -> list[dict[str, str]]:
    """Перекладывает строки из колонок файла в ключи полей записи."""
    if not mapping:
        return [dict(row) for row in rows]
    targets = {column: target for column, target in mapping.items() if target}
    mapped_rows: list[dict[str, str]] = []
    for row in rows:
        mapped: dict[str, str] = {}
        for column, target in targets.items():
            if column not in row:
                continue
            value = str(row[column] or "").strip()
            if not value:
                continue
            if mapped.get(target):
                mapped[target] = f"{mapped[target]}; {value}"
            else:
                mapped[target] = value
        mapped_rows.append(mapped)
    return mapped_rows


def validate_rows(
    rows: list[dict[str, str]],
    record_type: RecordType | str | None = None,
    *,
    known_titles: set[str] | None = None,
) -> ValidationResult:
    """Проверяет обязательные поля, тип записи и дубликаты названий."""
    rtype = normalize_record_type(record_type or RecordType.OTHER)
    required = [(item.key, item.label) for item in required_field_definitions(rtype)]
    known = {title.casefold() for title in (known_titles or set())}
    result = ValidationResult(total_rows=len(rows))
    seen: dict[str, int] = {}

    for index, row in enumerate(rows, start=1):
        problems: list[RowError] = []
        title = str(row.get("title") or "").strip()
        if not title:
            problems.append(RowError(index, "title", "Не заполнено название записи."))
        for key, label in required:
            if not str(row.get(key) or "").strip():
                problems.append(RowError(index, key, f"Не заполнено обязательное поле «{label}»."))

        raw_type = str(row.get("type") or "").strip()
        if raw_type:
            try:
                normalize_record_type(raw_type)
            except FieldValidationError:
                problems.append(RowError(index, "type", f"Неизвестный тип записи: {raw_type}."))

        if title:
            key = title.casefold()
            if key in seen:
                result.duplicates += 1
                problems.append(
                    RowError(index, "title", f"Дубликат названия (строка {seen[key]}).")
                )
            else:
                seen[key] = index
            if key in known:
                result.duplicates += 1
                problems.append(
                    RowError(index, "title", "Запись с таким названием уже есть в категории.")
                )

        if problems:
            result.invalid_rows += 1
            if len(result.errors) < MAX_ERRORS:
                result.errors.extend(problems[: MAX_ERRORS - len(result.errors)])
        else:
            result.valid_rows += 1
    return result


async def _existing_titles(session: AsyncSession | None, category_code: str) -> set[str]:
    if session is None or not category_code:
        return set()
    from app.services.records.service import get_category_by_code

    category = await get_category_by_code(session, category_code)
    if category is None:
        raise PreviewError(f"Категория {category_code} не найдена.")
    result = await session.execute(
        select(Record.title).where(
            Record.category_id == category.id,
            Record.status == "ACTIVE",
        )
    )
    return {str(title) for title in result.scalars().all() if title}


def _resolve_record_type(record_type: RecordType | str | None) -> RecordType:
    try:
        return normalize_record_type(record_type or RecordType.OTHER)
    except FieldValidationError as exc:
        raise PreviewError(str(exc)) from exc


def _sample_rows(rows: list[dict[str, str]], mapping: dict[str, str]) -> list[dict[str, str]]:
    sample: list[dict[str, str]] = []
    for row in rows:
        mapped = apply_field_mapping([row], mapping)[0] if mapping else dict(row)
        if not mapped:
            continue
        sample.append({key: value for key, value in mapped.items() if value})
        if len(sample) >= MAX_PREVIEW_ROWS:
            break
    return sample


def _decode_error_message(exc: Exception) -> str:
    return f"Не удалось разобрать файл: {exc}"


async def preview_import(
    session: AsyncSession | None,
    file_bytes: bytes,
    filename: str,
    category_code: str,
    *,
    record_type: RecordType | str | None = None,
    mapping: dict[str, str] | None = None,
) -> ImportPreview:
    """Разбирает файл, предлагает сопоставление полей и считает статистику.

    ``session`` может быть ``None`` — тогда проверка категории и дубликатов
    с существующими записями пропускается (удобно для тестов).
    """
    if not file_bytes:
        raise PreviewFormatError("Файл пуст.")
    try:
        source_format = detect_format(file_bytes, filename)
        encoding = detect_encoding(file_bytes) if source_format in TEXT_FORMATS else None
        table: ParsedTable = parse_file(
            file_bytes, filename, format_name=source_format, encoding=encoding
        )
    except Exception as exc:
        raise PreviewFormatError(_decode_error_message(exc)) from exc

    if not table.headers:
        raise PreviewFormatError("В файле не найдены заголовки колонок.")

    rtype = _resolve_record_type(record_type)
    if mapping is not None:
        suggested = dict(mapping)
    elif table.format == FORMAT_TXT and table.headers == ["text"]:
        # Построчный TXT без разделителей: каждая строка — название записи.
        suggested = {"text": "title"}
    else:
        suggested = suggest_field_mapping(table.headers, rtype)
    mapped_rows = apply_field_mapping(table.rows, suggested)
    known_titles = await _existing_titles(session, category_code)
    validation = validate_rows(mapped_rows, rtype, known_titles=known_titles)

    return ImportPreview(
        total_rows=table.row_count,
        valid_rows=validation.valid_rows,
        invalid_rows=validation.invalid_rows,
        errors=validation.errors[:MAX_PREVIEW_ERRORS],
        suggested_mapping=suggested,
        headers=table.headers,
        sample_rows=_sample_rows(table.rows, suggested),
        source_format=table.format,
        encoding=table.encoding or encoding,
        record_type=rtype.value,
        duplicate_rows=validation.duplicates,
        errors_truncated=len(validation.errors) > MAX_PREVIEW_ERRORS,
    )


def decode_preview_text(file_bytes: bytes, encoding: str | None = None) -> str:
    """Декодирует файл для отладочного отображения (используется в тестах)."""
    return decode_bytes(file_bytes, encoding or detect_encoding(file_bytes))
