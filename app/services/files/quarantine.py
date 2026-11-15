"""Карантин файлов: хранение до проверки и выпуск после чистого скана."""

import asyncio
from dataclasses import dataclass
from io import BytesIO
from uuid import UUID, uuid4

from minio import Minio
from minio.commonconfig import CopySource
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database.models import File, ScanStatus
from app.services.files.metadata import (
    calculate_sha256,
    detect_mime_type,
    get_extension,
    sanitize_filename,
)

QUARANTINE_PREFIX = "quarantine/"
RELEASED_PREFIX = "files/"

# Статусы, при которых файл нельзя выдавать сотрудникам.
_UNSAFE_SCAN_STATUSES = frozenset(
    {ScanStatus.PENDING, ScanStatus.INFECTED, ScanStatus.ERROR}
)


class QuarantineError(RuntimeError):
    """Ошибка карантина или перемещения файла в хранилище."""


@dataclass(frozen=True)
class QuarantineEntry:
    """Объект в карантине, ещё не привязанный к записи в базе."""

    bucket: str
    storage_key: str
    original_name: str
    mime_type: str
    size: int
    sha256: str


def get_minio_client() -> Minio:
    endpoint = settings.minio_endpoint.replace("http://", "").replace("https://", "")
    return Minio(
        endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure or settings.minio_endpoint.startswith("https"),
    )


def get_bucket_name(bucket: str | None = None) -> str:
    return bucket or getattr(settings, "minio_bucket", None) or "office-vault"


def build_storage_key(prefix: str, filename: str | None) -> str:
    """Собирает уникальный ключ объекта с очищенным именем файла."""
    clean_name = sanitize_filename(filename)
    clean_prefix = str(prefix or "").strip("/")
    key = f"{clean_prefix}/{uuid4().hex}/{clean_name}"
    if not clean_prefix:
        key = f"{uuid4().hex}/{clean_name}"
    return key[:1024]


def is_quarantine_key(storage_key: str | None) -> bool:
    return str(storage_key or "").lstrip("/").startswith(QUARANTINE_PREFIX)


def ensure_bucket(client: Minio, bucket: str) -> None:
    if client.bucket_exists(bucket):
        return
    try:
        client.make_bucket(bucket)
    except Exception:  # гонка при создании бакета
        if not client.bucket_exists(bucket):
            raise


def put_bytes(
    bucket: str,
    storage_key: str,
    data: bytes,
    content_type: str,
    *,
    client: Minio | None = None,
) -> None:
    client = client or get_minio_client()
    ensure_bucket(client, bucket)
    client.put_object(
        bucket,
        storage_key,
        BytesIO(data),
        length=len(data),
        content_type=content_type,
    )


def read_bytes(bucket: str, storage_key: str, *, client: Minio | None = None) -> bytes:
    client = client or get_minio_client()
    response = client.get_object(bucket, storage_key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def move_object(
    bucket: str,
    source_key: str,
    destination_key: str,
    content_type: str,
    *,
    client: Minio | None = None,
) -> None:
    """Копирует объект в итоговое место и удаляет карантинную копию."""
    if source_key == destination_key:
        return
    client = client or get_minio_client()
    ensure_bucket(client, bucket)
    client.copy_object(
        bucket,
        destination_key,
        CopySource(bucket, source_key),
    )
    client.remove_object(bucket, source_key)


def _as_uuid(value: UUID | str | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


async def quarantine_file(
    file_bytes: bytes,
    filename: str,
    session: AsyncSession,
    *,
    uploaded_by: UUID | None = None,
    mime_type: str | None = None,
    sha256: str | None = None,
    bucket: str | None = None,
    storage_key: str | None = None,
) -> File | QuarantineEntry:
    """Помещает файл в карантин.

    Если передан uploaded_by, создаётся запись File со статусом PENDING.
    Без uploaded_by возвращается только описание объекта в карантине.
    """
    data = bytes(file_bytes or b"")
    clean_name = sanitize_filename(filename)
    digest = sha256 or calculate_sha256(data)
    resolved_mime = mime_type or detect_mime_type(data, clean_name)
    resolved_bucket = get_bucket_name(bucket)
    key = storage_key or build_storage_key(QUARANTINE_PREFIX, clean_name)

    await asyncio.to_thread(
        put_bytes,
        resolved_bucket,
        key,
        data,
        resolved_mime,
    )

    if uploaded_by is None:
        return QuarantineEntry(
            bucket=resolved_bucket,
            storage_key=key,
            original_name=clean_name,
            mime_type=resolved_mime,
            size=len(data),
            sha256=digest,
        )

    file = File(
        original_name=clean_name,
        storage_key=key,
        bucket=resolved_bucket,
        mime_type=resolved_mime,
        extension=get_extension(clean_name),
        size=len(data),
        sha256=digest,
        scan_status=ScanStatus.PENDING,
        uploaded_by=uploaded_by,
    )
    session.add(file)
    await session.flush()
    return file


async def release_from_quarantine(
    file_id: UUID | str,
    session: AsyncSession,
) -> File | None:
    """Выпускает файл из карантина: переносит объект и ставит статус CLEAN."""
    resolved_id = _as_uuid(file_id)
    if resolved_id is None:
        return None
    file = await session.get(File, resolved_id)
    if file is None:
        return None
    if file.scan_status == ScanStatus.INFECTED:
        raise QuarantineError("Заражённый файл нельзя выпустить из карантина.")
    if file.deleted_at is not None:
        raise QuarantineError("Файл удалён и не может быть выпущен из карантина.")

    if is_quarantine_key(file.storage_key):
        destination_key = build_storage_key(RELEASED_PREFIX, file.original_name)
        try:
            await asyncio.to_thread(
                move_object,
                file.bucket,
                file.storage_key,
                destination_key,
                file.mime_type,
            )
        except Exception as exc:  # хранилище недоступно
            raise QuarantineError(f"Не удалось переместить файл из карантина: {exc}") from exc
        file.storage_key = destination_key

    file.scan_status = ScanStatus.CLEAN
    await session.commit()
    await session.refresh(file)
    return file


async def is_quarantined(file_id: UUID | str, session: AsyncSession) -> bool:
    """True, если файл ещё не выпущен и не должен выдаваться сотрудникам."""
    resolved_id = _as_uuid(file_id)
    if resolved_id is None:
        return False
    file = await session.get(File, resolved_id)
    if file is None:
        return False
    if is_quarantine_key(file.storage_key):
        return True
    return file.scan_status in _UNSAFE_SCAN_STATUSES
