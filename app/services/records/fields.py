"""Динамические поля записей: типы, валидация, шифрование и отображение."""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.encryption import decrypt_secret, encrypt_secret
from app.database.models import FieldType, RecordField, RecordType

# Секреты в карточке записи никогда не показываются без отдельного подтверждения.
SECRET_MASK = "••••••••"
SECRET_FIELD_TYPES = frozenset({FieldType.PASSWORD, FieldType.SECRET})

MAX_TEXT_LENGTH = 2000
MAX_LONG_TEXT_LENGTH = 20000
MAX_SECRET_LENGTH = 4096
MAX_URL_LENGTH = 2048
MAX_EMAIL_LENGTH = 254
MAX_PHONE_LENGTH = 32
MAX_JSON_LENGTH = 20000
MAX_FILE_REFERENCE_LENGTH = 1024
MAX_MULTISELECT_ITEMS = 50
MAX_MULTISELECT_ITEM_LENGTH = 64
MAX_LABEL_LENGTH = 128

FIELD_TYPE_LABELS: dict[FieldType, str] = {
    FieldType.TEXT: "Текст",
    FieldType.LONG_TEXT: "Длинный текст",
    FieldType.PASSWORD: "Пароль",
    FieldType.SECRET: "Секрет",
    FieldType.URL: "Ссылка",
    FieldType.EMAIL: "Email",
    FieldType.PHONE: "Телефон",
    FieldType.NUMBER: "Число",
    FieldType.BOOLEAN: "Да/Нет",
    FieldType.DATE: "Дата",
    FieldType.DATETIME: "Дата и время",
    FieldType.JSON: "JSON",
    FieldType.FILE_REFERENCE: "Файл",
    FieldType.MULTISELECT: "Множественный выбор",
}

FIELD_TYPE_HINTS: dict[FieldType, str] = {
    FieldType.TEXT: "Введите текст.",
    FieldType.LONG_TEXT: "Введите текст (можно несколько строк).",
    FieldType.PASSWORD: "Введите пароль. Значение будет зашифровано.",
    FieldType.SECRET: "Введите секрет. Значение будет зашифровано.",
    FieldType.URL: "Введите ссылку, например https://example.com.",
    FieldType.EMAIL: "Введите email, например user@example.com.",
    FieldType.PHONE: "Введите телефон, например +7 (999) 123-45-67.",
    FieldType.NUMBER: "Введите число, например 42 или 3,14.",
    FieldType.BOOLEAN: "Введите да/нет (или true/false).",
    FieldType.DATE: "Введите дату, например 15.11.2026.",
    FieldType.DATETIME: "Введите дату и время, например 15.11.2026 14:30.",
    FieldType.JSON: 'Введите JSON, например {"ключ": "значение"}.',
    FieldType.FILE_REFERENCE: "Введите идентификатор файла или имя объекта.",
    FieldType.MULTISELECT: "Введите значения через запятую.",
}

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_ALLOWED_RE = re.compile(r"^\+?[0-9\s().-]+$")
_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y")
_DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
)
_TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on", "да", "+"})
_FALSE_VALUES = frozenset({"0", "false", "no", "n", "off", "нет", "-"})
_FILE_REFERENCE_KEYS = frozenset(
    {"file_id", "object_name", "bucket", "filename", "size", "mime_type"}
)


class FieldValidationError(ValueError):
    """Некорректное значение или описание динамического поля."""


class MissingRequiredField(FieldValidationError):
    """Не заполнено обязательное поле типа записи."""

    def __init__(self, labels: Sequence[str]):
        self.labels = tuple(labels)
        joined = ", ".join(self.labels)
        super().__init__(f"Заполните обязательные поля: {joined}.")


@dataclass(frozen=True)
class FieldDefinition:
    """Описание поля, которое предлагается при создании записи."""

    key: str
    label: str
    field_type: FieldType
    required: bool = False
    hint: str | None = None


@dataclass(frozen=True)
class FieldInput:
    """Значение поля из сервисного слоя или FSM."""

    key: str
    value: Any
    field_type: FieldType = FieldType.TEXT
    label: str | None = None
    encrypted: bool = False


@dataclass(frozen=True)
class EncodedField:
    """Значение, разложенное по колонкам record_fields."""

    value_text: str | None = None
    value_json: Any | None = None
    encrypted_value: str | None = None
    raw_value: Any | None = None


DEFAULT_FIELD_DEFINITIONS: dict[RecordType, tuple[FieldDefinition, ...]] = {
    RecordType.CREDENTIALS: (
        FieldDefinition("login", "Логин", FieldType.TEXT, required=True),
        FieldDefinition("password", "Пароль", FieldType.PASSWORD, required=True),
        FieldDefinition("url", "Ссылка", FieldType.URL),
        FieldDefinition("notes", "Заметки", FieldType.LONG_TEXT),
    ),
    RecordType.SIP: (
        FieldDefinition("sip_username", "SIP Username", FieldType.TEXT, required=True),
        FieldDefinition("sip_password", "SIP Password", FieldType.PASSWORD, required=True),
        FieldDefinition("server", "Сервер", FieldType.TEXT),
        FieldDefinition("port", "Порт", FieldType.NUMBER),
    ),
    RecordType.LINKS: (
        FieldDefinition("url", "Ссылка", FieldType.URL, required=True),
        FieldDefinition("description", "Описание", FieldType.LONG_TEXT),
    ),
    RecordType.SETTINGS: (
        FieldDefinition("parameter", "Параметр", FieldType.TEXT, required=True),
        FieldDefinition("value", "Значение", FieldType.LONG_TEXT),
        FieldDefinition("config", "Конфигурация", FieldType.JSON),
    ),
    RecordType.INSTRUCTIONS: (
        FieldDefinition("content", "Содержание", FieldType.LONG_TEXT, required=True),
        FieldDefinition("source_url", "Источник", FieldType.URL),
    ),
    RecordType.FILES: (),
    RecordType.OTHER: (),
}


def default_field_definitions(record_type: RecordType | str) -> tuple[FieldDefinition, ...]:
    """Возвращает шаблон полей для типа записи."""
    return DEFAULT_FIELD_DEFINITIONS.get(normalize_record_type(record_type), ())


def required_field_definitions(record_type: RecordType | str) -> tuple[FieldDefinition, ...]:
    return tuple(item for item in default_field_definitions(record_type) if item.required)


def normalize_record_type(record_type: RecordType | str) -> RecordType:
    if isinstance(record_type, RecordType):
        return record_type
    try:
        return RecordType(str(record_type).strip().upper())
    except ValueError as exc:
        raise FieldValidationError(f"Неизвестный тип записи: {record_type}.") from exc


def normalize_field_type(value: FieldType | str) -> FieldType:
    if isinstance(value, FieldType):
        return value
    try:
        return FieldType(str(value).strip().upper())
    except ValueError as exc:
        raise FieldValidationError(f"Неизвестный тип поля: {value}.") from exc


def normalize_field_key(key: str) -> str:
    """Приводит ключ поля к безопасному виду для БД и callback_data."""
    raw = str(key or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    if not slug:
        raise FieldValidationError("Ключ поля не может быть пустым.")
    if not slug[0].isalpha():
        slug = f"field_{slug}"
    slug = slug[:64].rstrip("_")
    if not _KEY_RE.match(slug):
        raise FieldValidationError("Ключ поля должен содержать латинские буквы, цифры и «_».")
    return slug


def normalize_field_label(label: str, *, fallback: str = "Поле") -> str:
    normalized = str(label or "").strip()
    if not normalized:
        normalized = fallback
    return normalized[:MAX_LABEL_LENGTH]


def humanize_field_label(key: str) -> str:
    return normalize_field_label(key.replace("_", " ").title())


def is_secret_field_type(field_type: FieldType | str) -> bool:
    return normalize_field_type(field_type) in SECRET_FIELD_TYPES


def is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise FieldValidationError(f"{label}: ожидается строка.")
    return value


def _validate_text(value: Any, max_length: int, label: str) -> str:
    text = _require_string(value, label).strip()
    if len(text) > max_length:
        raise FieldValidationError(f"{label}: не более {max_length} символов.")
    return text


def _validate_secret(value: Any, label: str) -> str:
    text = _require_string(value, label)
    if len(text) > MAX_SECRET_LENGTH:
        raise FieldValidationError(f"{label}: не более {MAX_SECRET_LENGTH} символов.")
    return text


def _validate_url(value: Any) -> str:
    from urllib.parse import urlsplit

    text = _validate_text(value, MAX_URL_LENGTH, "Ссылка")
    if any(char.isspace() for char in text):
        raise FieldValidationError("Ссылка не должна содержать пробелы.")
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise FieldValidationError("Ссылка должна начинаться с http:// или https://.")
    return text


def _validate_email(value: Any) -> str:
    text = _validate_text(value, MAX_EMAIL_LENGTH, "Email")
    if not _EMAIL_RE.match(text):
        raise FieldValidationError("Некорректный email.")
    return text


def _validate_phone(value: Any) -> str:
    text = _validate_text(value, MAX_PHONE_LENGTH, "Телефон")
    if not _PHONE_ALLOWED_RE.match(text):
        raise FieldValidationError("Телефон может содержать цифры, «+», пробелы, скобки и дефис.")
    digits = sum(char.isdigit() for char in text)
    if not 6 <= digits <= 15:
        raise FieldValidationError("Телефон должен содержать от 6 до 15 цифр.")
    return text


def _validate_number(value: Any) -> str:
    if isinstance(value, bool):
        raise FieldValidationError("Число не может быть логическим значением.")
    raw = str(value).strip().replace(" ", "").replace(",", ".")
    if not raw:
        raise FieldValidationError("Введите число.")
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise FieldValidationError("Некорректное число.") from exc
    if not number.is_finite():
        raise FieldValidationError("Число должно быть конечным.")
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.normalize(), "f")


def _validate_boolean(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and value in (0, 1):
        return "true" if value else "false"
    raw = str(value).strip().lower()
    if raw in _TRUE_VALUES:
        return "true"
    if raw in _FALSE_VALUES:
        return "false"
    raise FieldValidationError("Введите да/нет (true/false).")


def _validate_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = _require_string(value, "Дата").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    raise FieldValidationError("Некорректная дата. Используйте 15.11.2026 или 2026-11-15.")


def _validate_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    raw = _require_string(value, "Дата и время").strip()
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(microsecond=0).isoformat()
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed.replace(microsecond=0).isoformat()
    except ValueError as exc:
        raise FieldValidationError(
            "Некорректная дата и время. Используйте 15.11.2026 14:30."
        ) from exc


def _validate_json(value: Any) -> Any:
    if isinstance(value, str):
        raw = value.strip()
        if len(raw) > MAX_JSON_LENGTH:
            raise FieldValidationError(f"JSON: не более {MAX_JSON_LENGTH} символов.")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FieldValidationError(f"Некорректный JSON: {exc.msg}.") from exc
    try:
        serialized = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise FieldValidationError("Значение не может быть сохранено как JSON.") from exc
    if len(serialized) > MAX_JSON_LENGTH:
        raise FieldValidationError(f"JSON: не более {MAX_JSON_LENGTH} символов.")
    return value


def _validate_file_reference(value: Any) -> str | dict[str, Any]:
    if isinstance(value, Mapping):
        cleaned = {
            key: item
            for key, item in value.items()
            if key in _FILE_REFERENCE_KEYS and item is not None
        }
        if not {"file_id", "object_name", "filename"} & set(cleaned):
            raise FieldValidationError("Ссылка на файл должна содержать file_id, object_name или filename.")
        for key, item in cleaned.items():
            if key == "size":
                try:
                    cleaned[key] = int(item)
                except (TypeError, ValueError) as exc:
                    raise FieldValidationError("Размер файла должен быть числом.") from exc
            elif not isinstance(item, str):
                raise FieldValidationError(f"Поле файла «{key}» должно быть строкой.")
        for key in ("file_id", "object_name", "filename", "bucket", "mime_type"):
            if key in cleaned and len(cleaned[key]) > MAX_FILE_REFERENCE_LENGTH:
                raise FieldValidationError(f"Поле файла «{key}» слишком длинное.")
        return cleaned
    text = _validate_text(value, MAX_FILE_REFERENCE_LENGTH, "Ссылка на файл")
    return text


def parse_multiselect(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = re.split(r"[,;\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    elif is_empty_value(value):
        return []
    else:
        raise FieldValidationError("Множественный выбор: ожидается список значений.")

    items: list[str] = []
    for part in parts:
        item = str(part).strip()
        if not item:
            continue
        if len(item) > MAX_MULTISELECT_ITEM_LENGTH:
            raise FieldValidationError(
                f"Элемент списка: не более {MAX_MULTISELECT_ITEM_LENGTH} символов."
            )
        if item not in items:
            items.append(item)
    if len(items) > MAX_MULTISELECT_ITEMS:
        raise FieldValidationError(f"Слишком много значений: максимум {MAX_MULTISELECT_ITEMS}.")
    return items


def validate_field_value(field_type: FieldType | str, value: Any) -> Any:
    """Проверяет и нормализует значение поля. Пустое значение даёт None."""
    normalized_type = normalize_field_type(field_type)
    if is_empty_value(value):
        return None

    if normalized_type is FieldType.TEXT:
        return _validate_text(value, MAX_TEXT_LENGTH, "Текст")
    if normalized_type is FieldType.LONG_TEXT:
        return _validate_text(value, MAX_LONG_TEXT_LENGTH, "Длинный текст")
    if normalized_type in SECRET_FIELD_TYPES:
        return _validate_secret(value, FIELD_TYPE_LABELS[normalized_type])
    if normalized_type is FieldType.URL:
        return _validate_url(value)
    if normalized_type is FieldType.EMAIL:
        return _validate_email(value)
    if normalized_type is FieldType.PHONE:
        return _validate_phone(value)
    if normalized_type is FieldType.NUMBER:
        return _validate_number(value)
    if normalized_type is FieldType.BOOLEAN:
        return _validate_boolean(value)
    if normalized_type is FieldType.DATE:
        return _validate_date(value)
    if normalized_type is FieldType.DATETIME:
        return _validate_datetime(value)
    if normalized_type is FieldType.JSON:
        return _validate_json(value)
    if normalized_type is FieldType.FILE_REFERENCE:
        return _validate_file_reference(value)
    if normalized_type is FieldType.MULTISELECT:
        return parse_multiselect(value)
    raise FieldValidationError(f"Неподдерживаемый тип поля: {normalized_type.value}.")


def encode_field_value(field_type: FieldType | str, value: Any) -> EncodedField:
    """Готовит значение для колонок value_text/value_json/encrypted_value."""
    normalized_type = normalize_field_type(field_type)
    normalized = validate_field_value(normalized_type, value)
    if normalized is None:
        return EncodedField(raw_value=None)

    if normalized_type in SECRET_FIELD_TYPES:
        return EncodedField(
            encrypted_value=encrypt_secret(normalized),
            raw_value=normalized,
        )
    if normalized_type is FieldType.JSON:
        return EncodedField(
            value_text=json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
            value_json=normalized,
            raw_value=normalized,
        )
    if normalized_type is FieldType.MULTISELECT:
        items = list(normalized)
        return EncodedField(
            value_text=", ".join(items),
            value_json=items,
            raw_value=items,
        )
    if normalized_type is FieldType.FILE_REFERENCE and isinstance(normalized, dict):
        identifier = (
            normalized.get("file_id")
            or normalized.get("object_name")
            or normalized.get("filename")
        )
        return EncodedField(
            value_text=str(identifier) if identifier else None,
            value_json=normalized,
            raw_value=normalized,
        )
    return EncodedField(value_text=str(normalized), raw_value=normalized)


def format_raw_field_value(field_type: FieldType | str, value: Any) -> str:
    """Показывает нормализованное значение без обращения к БД."""
    normalized_type = normalize_field_type(field_type)
    if normalized_type in SECRET_FIELD_TYPES:
        return SECRET_MASK if not is_empty_value(value) else "—"
    if is_empty_value(value):
        return "—"
    if normalized_type is FieldType.BOOLEAN:
        return "Да" if str(value).lower() in {"true", "1"} else "Нет"
    if normalized_type is FieldType.DATE:
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            return str(value)
    if normalized_type is FieldType.DATETIME:
        try:
            return datetime.fromisoformat(str(value)).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return str(value)
    if normalized_type is FieldType.JSON:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if normalized_type is FieldType.MULTISELECT:
        if isinstance(value, (list, tuple, set)):
            return ", ".join(str(item) for item in value) or "—"
        return str(value)
    if normalized_type is FieldType.FILE_REFERENCE and isinstance(value, Mapping):
        return str(
            value.get("filename")
            or value.get("file_id")
            or value.get("object_name")
            or "—"
        )
    return str(value)


def format_field_value(
    field_type: FieldType | str,
    *,
    value_text: str | None = None,
    value_json: Any | None = None,
    encrypted_value: str | None = None,
    reveal: bool = False,
) -> str:
    """Форматирует значение поля карточки, маскируя секреты по умолчанию."""
    normalized_type = normalize_field_type(field_type)
    if normalized_type in SECRET_FIELD_TYPES:
        if not encrypted_value:
            return "—"
        if not reveal:
            return SECRET_MASK
        return decrypt_secret(encrypted_value)
    if value_json is not None:
        return format_raw_field_value(normalized_type, value_json)
    return format_raw_field_value(normalized_type, value_text)


def format_record_field(field: RecordField, *, reveal: bool = False) -> str:
    """Удобная обёртка для RecordField из карточки записи."""
    return format_field_value(
        field.field_type,
        value_text=field.value_text,
        value_json=field.value_json,
        encrypted_value=field.encrypted_value,
        reveal=reveal,
    )


def is_secret_field(field: RecordField) -> bool:
    return bool(field.encrypted_value) or is_secret_field_type(field.field_type)


def mask_secret(value: str | None) -> str:
    return SECRET_MASK if value else "—"


def validate_required_fields(
    record_type: RecordType | str,
    values: Mapping[str, Any],
    *,
    definitions: Sequence[FieldDefinition] | None = None,
) -> None:
    """Проверяет обязательные поля типа записи."""
    items = definitions if definitions is not None else default_field_definitions(record_type)
    missing = [
        item.label
        for item in items
        if item.required and is_empty_value(values.get(item.key))
    ]
    if missing:
        raise MissingRequiredField(missing)


def field_definition_map(record_type: RecordType | str) -> dict[str, FieldDefinition]:
    return {item.key: item for item in default_field_definitions(record_type)}


__all__ = [
    "DEFAULT_FIELD_DEFINITIONS",
    "FIELD_TYPE_HINTS",
    "FIELD_TYPE_LABELS",
    "SECRET_FIELD_TYPES",
    "SECRET_MASK",
    "EncodedField",
    "FieldDefinition",
    "FieldInput",
    "FieldValidationError",
    "MissingRequiredField",
    "default_field_definitions",
    "encode_field_value",
    "field_definition_map",
    "format_field_value",
    "format_raw_field_value",
    "format_record_field",
    "humanize_field_label",
    "is_empty_value",
    "is_secret_field",
    "is_secret_field_type",
    "mask_secret",
    "normalize_field_key",
    "normalize_field_label",
    "normalize_field_type",
    "normalize_record_type",
    "parse_multiselect",
    "required_field_definitions",
    "validate_field_value",
    "validate_required_fields",
]
