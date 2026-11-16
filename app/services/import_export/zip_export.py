"""ZIP-экспорт: записи, вложения и манифест с контрольными суммами (Phase 08).

Состав архива:

- ``records.json`` — записи в том же JSON-формате, что и отдельный экспорт в JSON;
- ``files/<file_id>_<имя>`` — вложения, которые пользователь имеет право скачать;
- ``manifest.json`` — метаданные записей и файлов, SHA-256 всех членов архива и список
  пропущенных вложений с причиной.

Файл, на который у пользователя нет права DOWNLOAD (или который недоступен в хранилище),
в архив не попадает, но фиксируется в манифесте — получатель видит, чего не хватает.
"""

import asyncio
import io
import json
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import File, PermissionCode, Record, User
from app.services.files.access import ACCESS_SUCCESS, log_file_access
from app.services.files.security import sanitize_path
from app.services.files.service import read_object
from app.services.import_export.export_formats import (
    JSON_DOCUMENT_FORMAT,
    JSON_DOCUMENT_VERSION,
    json_bytes,
)
from app.services.import_export.rows import build_table
from app.services.permissions.service import TARGET_FILE, PermissionService
from app.services.records.service import list_attachments

MANIFEST_VERSION = 1
RECORDS_MEMBER = "records.json"
MANIFEST_MEMBER = "manifest.json"
FILES_PREFIX = "files"


@dataclass(frozen=True)
class ZipExport:
    """Готовый архив и итоги для отчёта."""

    data: bytes
    record_count: int
    file_count: int
    skipped_files: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def assemble_zip(members: Sequence[tuple[str, bytes]]) -> bytes:
    """Упаковывает члены архива в zip; порядок сохраняется, пути проверяются на выход."""
    buffer = io.BytesIO()
    seen: set[str] = set()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members:
            if name.startswith("/") or ".." in name.split("/") or name in seen:
                raise ValueError(f"Недопустимое имя члена архива: {name}")
            seen.add(name)
            archive.writestr(name, payload)
    return buffer.getvalue()


def checksum(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def build_manifest(
    *,
    generated_at: datetime,
    records: Sequence[dict[str, Any]],
    files: Sequence[dict[str, Any]],
    skipped_files: Sequence[dict[str, Any]],
    include_files: bool,
    include_secrets: bool,
    checksums: dict[str, str],
) -> dict[str, Any]:
    """Манифест архива: состав, контрольные суммы и причины пропусков."""
    return {
        "format": JSON_DOCUMENT_FORMAT,
        "manifest_version": MANIFEST_VERSION,
        "document_version": JSON_DOCUMENT_VERSION,
        "generated_at": generated_at.isoformat(),
        "include_files": include_files,
        "include_secrets": include_secrets,
        "totals": {
            "records": len(records),
            "files": len(files),
            "skipped_files": len(skipped_files),
        },
        "records": list(records),
        "files": list(files),
        "skipped_files": list(skipped_files),
        "checksums": dict(sorted(checksums.items())),
    }


def _record_summary(record: Record) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "title": record.title,
        "type": record.record_type.value if record.record_type else "",
        "category": record.category.code if record.category else "",
    }


async def _load_file(file: File) -> bytes:
    return await asyncio.to_thread(read_object, file.bucket, file.storage_key)


async def build_zip_export(
    session: AsyncSession,
    user: User,
    records: Sequence[Record],
    include_files: bool,
    *,
    fields: Sequence[str] | None = None,
    include_secrets: bool = False,
    generated_at: datetime | None = None,
) -> ZipExport:
    """Собирает архив и возвращает данные вместе с итогами.

    ``records`` должны быть уже отфильтрованы по праву VIEW вызывающим кодом.
    Права на каждый файл проверяются здесь же (DOWNLOAD включает статус CLEAN).
    """
    moment = generated_at or datetime.now(UTC)
    user_id = user.id

    attachments: dict[UUID, list[File]] = {}
    if include_files:
        for record in records:
            attachments[record.id] = await list_attachments(session, record.id)

    names = {rid: [item.original_name for item in items] for rid, items in attachments.items()}
    table = build_table(
        list(records),
        fields=fields,
        include_secrets=include_secrets,
        attachments=names if include_files else None,
    )
    records_payload = json_bytes(table, generated_at=moment)

    file_members: list[tuple[str, bytes]] = []
    file_entries: dict[UUID, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    # Файл, привязанный к нескольким записям, проверяется и кладётся в архив один раз.
    decided: dict[UUID, bool] = {}
    for record in records:
        for item in attachments.get(record.id, []):
            if item.id in decided:
                if decided[item.id]:
                    file_entries[item.id]["records"].append(str(record.id))
                continue
            allowed = await PermissionService.can(
                user, PermissionCode.DOWNLOAD, TARGET_FILE, item.id, session
            )
            if not allowed:
                decided[item.id] = False
                skipped.append(
                    _skipped(item, record, "Нет права скачивания или файл не прошёл проверку")
                )
                continue
            try:
                payload = await _load_file(item)
            except Exception:  # noqa: BLE001 — хранилище недоступно, архив всё равно собираем
                decided[item.id] = False
                skipped.append(_skipped(item, record, "Файл недоступен в хранилище"))
                continue
            decided[item.id] = True
            path = f"{FILES_PREFIX}/{item.id}_{sanitize_path(item.original_name)}"
            file_members.append((path, payload))
            file_entries[item.id] = {
                "file_id": str(item.id),
                "name": item.original_name,
                "path": path,
                "size": len(payload),
                "sha256": checksum(payload),
                "mime_type": item.mime_type,
                "records": [str(record.id)],
            }
            await log_file_access(item.id, user_id, "EXPORT", ACCESS_SUCCESS, session)

    files_manifest = list(file_entries.values())
    records_manifest = []
    for record in records:
        summary = _record_summary(record)
        summary["files"] = [
            str(item.id) for item in attachments.get(record.id, []) if decided.get(item.id)
        ]
        records_manifest.append(summary)

    checksums = {RECORDS_MEMBER: checksum(records_payload)}
    checksums.update({path: checksum(payload) for path, payload in file_members})
    manifest = build_manifest(
        generated_at=moment,
        records=records_manifest,
        files=files_manifest,
        skipped_files=skipped,
        include_files=include_files,
        include_secrets=include_secrets,
        checksums=checksums,
    )
    manifest_payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

    members = [
        (RECORDS_MEMBER, records_payload),
        *file_members,
        (MANIFEST_MEMBER, manifest_payload),
    ]
    return ZipExport(
        data=assemble_zip(members),
        record_count=len(records),
        file_count=len(file_members),
        skipped_files=tuple(skipped),
    )


def _skipped(item: File, record: Record, reason: str) -> dict[str, Any]:
    return {
        "file_id": str(item.id),
        "name": item.original_name,
        "record_id": str(record.id),
        "reason": reason,
    }


async def create_zip_export(
    session: AsyncSession,
    user: User,
    records: Sequence[Record],
    include_files: bool,
    *,
    fields: Sequence[str] | None = None,
    include_secrets: bool = False,
) -> bytes:
    """Создаёт ZIP с файлами и манифестом и возвращает его байты."""
    export = await build_zip_export(
        session,
        user,
        records,
        include_files,
        fields=fields,
        include_secrets=include_secrets,
    )
    return export.data
