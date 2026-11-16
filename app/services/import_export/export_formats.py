"""Сериализация таблицы экспорта в CSV, JSON и XLSX (чистые функции).

Модуль отделён от ``formats.py``, где живут парсеры импорта.
"""

import csv
import io
import json
from datetime import datetime
from typing import Any

from app.services.import_export.rows import ExportTable
from app.services.import_export.xlsx_writer import build_xlsx

EXPORT_FORMATS: tuple[str, ...] = ("csv", "xlsx", "json", "zip")
FORMAT_EXTENSIONS: dict[str, str] = {"csv": "csv", "xlsx": "xlsx", "json": "json", "zip": "zip"}
FORMAT_MIME_TYPES: dict[str, str] = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "json": "application/json",
    "zip": "application/zip",
}
JSON_DOCUMENT_FORMAT = "office-vault-export"
JSON_DOCUMENT_VERSION = 1

# Ячейка, начинающаяся с этих символов, таблицы-редакторы выполнят как формулу (CSV injection).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def neutralize_cell(value: Any) -> Any:
    """Экранирует формулы: значение с опасным первым символом получает префикс-апостроф.

    Срабатывает и на телефоны вида «+7…», это осознанная цена за безопасность таблиц.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def csv_bytes(table: ExportTable) -> bytes:
    """CSV в UTF-8 с BOM: так русский текст корректно открывается в Excel."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(table.columns)
    for row in table.rows:
        writer.writerow([neutralize_cell(row.get(column, "")) for column in table.columns])
    return buffer.getvalue().encode("utf-8-sig")


def json_bytes(table: ExportTable, *, generated_at: datetime) -> bytes:
    """JSON-документ экспорта: заголовок, список колонок и записи.

    Значения не экранируются формулами: JSON не открывается как таблица.
    """
    document = {
        "format": JSON_DOCUMENT_FORMAT,
        "version": JSON_DOCUMENT_VERSION,
        "generated_at": generated_at.isoformat(),
        "columns": list(table.columns),
        "record_count": len(table.rows),
        "records": [
            {column: row.get(column, "") for column in table.columns} for row in table.rows
        ],
    }
    return json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")


def xlsx_bytes(table: ExportTable) -> bytes:
    """Книга XLSX с одним листом; заголовок закреплён, ячейки защищены от формул."""
    body = [
        [neutralize_cell(row.get(column, "")) for column in table.columns] for row in table.rows
    ]
    return build_xlsx(list(table.columns), body)
