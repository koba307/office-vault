"""Сервис файлов (Phase 06): безопасная загрузка, сканирование, жизненный цикл.

Порядок обработки при загрузке важен и идёт от дешёвого к дорогому:

1. ``inspect_upload`` — локальные проверки безопасности (размер, имя файла,
   magic bytes, ZIP bomb) до любых обращений к хранилищу и сканеру.
2. ``scan_content`` — антивирусная проверка ClamAV; результат всегда
   фиксируется в ``FileScanResult`` (интеграция со сканером — аудитабельна).
3. Только «чистый» файл попадает в бакет; заражённый остаётся в реестре
   ``files`` со статусом INFECTED, но байты в хранилище не пишутся.

Права на скачивание (DOWNLOAD) даёт только статус CLEAN — это проверяет
PermissionService (см. app/services/permissions/file.py), поэтому файлы с
SKIPPED/ERROR/PENDING видны в карточке, но не выдаются сотрудникам.
"""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from uuid import UUID

from minio import Minio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.database.models import (
    File,
    FileScanResult,
    PermissionCode,
    Record,
    RecordType,
    ScanStatus,
    User,
)
from app.services.auth.permissions import log_audit
from app.services.files.access import (
    ACCESS_DENIED,
    ACCESS_ERROR,
    ACCESS_SUCCESS,
    log_file_access,
)
from app.services.files.metadata import (
    OLE_MIME_TYPES,
    ZIP_CONTAINER_MIME_TYPES,
    calculate_sha256,
    check_size_limit,
    detect_mime_type,
    normalize_mime_type,
    sanitize_filename,
    validate_extension,
)
from app.services.files.quarantine import (
    QuarantineError,
    quarantine_file,
    release_from_quarantine,
)
from app.services.files.security import (
    DEFAULT_ZIP_MAX_RATIO,
    DEFAULT_ZIP_MAX_UNCOMPRESSED,
    check_zip_bomb,
    detect_magic_mime,
    sanitize_path,
    validate_magic_bytes,
)
from app.services.permissions.errors import DenyReason, PermissionDenied
from app.services.permissions.service import TARGET_CATEGORY, TARGET_FILE, PermissionService
from app.services.records.service import add_attachment, create_record

# Ограничение на размер загружаемого файла (совпадает с лимитом Bot API).
MAX_FILE_BYTES = 25 * 1024 * 1024
GENERIC_MIME = "application/octet-stream"


class FileError(ValueError):
    """Базовая ошибка домена файлов."""


class FileSecurityError(FileError):
    """Файл не прошёл локальные проверки безопасности."""


class FileTooLargeError(FileSecurityError):
    """Размер файла превышает разрешённый лимит."""


class FileInfectedError(FileError):
    """Файл заражён: в хранилище не попадает, остаётся в реестре."""


class FileStateError(FileError):
    """Текущее состояние файла не допускает операцию."""


class FileValidationError(FileSecurityError):
    """Файл не прошёл проверку метаданных MIME/расширения."""


class FileTooLarge(FileTooLargeError):
    """Размер файла больше лимита (алиас с понятным именем проверки)."""


class FileMimeMismatch(FileValidationError):
    """Фактический MIME-тип не совпадает с заявленным."""


class FileExtensionMismatch(FileValidationError):
    """Расширение файла не соответствует его содержимому."""


class FilePermissionError(FileError):
    """У пользователя нет прав на загрузку файла."""


class FileRecordNotFound(FileStateError):
    """Запись о файле не найдена."""


class FileScanError(FileStateError):
    """Файл нельзя выпустить из карантина после проверки."""


def get_minio_client() -> Minio:
    endpoint = settings.minio_endpoint.replace("http://", "").replace("https://", "")
    return Minio(
        endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_endpoint.startswith("https"),
    )


def bucket_name() -> str:
    return getattr(settings, "minio_bucket", None) or "office-vault"


def max_upload_bytes() -> int:
    return getattr(settings, "max_upload_bytes", None) or MAX_FILE_BYTES


def zip_bomb_limits() -> tuple[float, int]:
    """(max_ratio, max_size) для проверки архивов; переопределяются настройками."""
    ratio = getattr(settings, "zip_max_ratio", None) or DEFAULT_ZIP_MAX_RATIO
    size = getattr(settings, "zip_max_uncompressed", None) or DEFAULT_ZIP_MAX_UNCOMPRESSED
    return float(ratio), int(size)


# ---------------------------------------------------------------------------
# Локальные проверки содержимого (без БД и хранилища)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadInspection:
    """Итог безопасной подготовки файла к приёму."""

    safe_name: str
    content_type: str
    size: int
    sha256: str
    extension: str | None


def inspect_upload(
    file_bytes: bytes,
    filename: str | None,
    content_type: str | None = None,
) -> UploadInspection:
    """Проверяет размер, имя (path traversal), magic bytes и ZIP bomb.

    Возвращает нормализованные метаданные для реестра и хранилища.
    При любой подозрительности поднимает ``FileSecurityError``.
    """
    data = bytes(file_bytes or b"")
    if not data:
        raise FileSecurityError("Пустой файл принять нельзя.")

    limit = max_upload_bytes()
    if len(data) > limit:
        raise FileTooLargeError(
            f"Файл слишком большой: {len(data)} байт (максимум {limit} байт)."
        )

    # Защита от path traversal: храним и показываем только безопасное имя.
    safe_name = sanitize_path(filename)

    bomb = check_zip_bomb(data, *zip_bomb_limits())
    if not bomb.safe:
        raise FileSecurityError(bomb.reason or "ZIP-архив не прошёл проверку на бомбу.")

    declared = str(content_type or "").split(";", 1)[0].strip().lower() or None
    if not validate_magic_bytes(data, declared):
        detected = detect_magic_mime(data) or "неизвестный формат"
        raise FileSecurityError(
            f"Содержимое файла не соответствует заявленному типу "
            f"«{declared}»; определён как {detected}."
        )

    mime = declared or detect_magic_mime(data) or GENERIC_MIME
    extension = safe_name.rsplit(".", 1)[1].lower()[:32] if "." in safe_name else None
    return UploadInspection(
        safe_name=safe_name,
        content_type=mime[:128],
        size=len(data),
        sha256=sha256(data).hexdigest(),
        extension=extension,
    )


@dataclass(frozen=True)
class PreparedUpload:
    """Нормализованные метаданные после строгой проверки MIME и расширения."""

    filename: str
    mime_type: str
    sha256: str
    size: int


GENERIC_MIME_TYPES = frozenset(
    {"", GENERIC_MIME, "binary/octet-stream", "application/unknown"}
)


def _mime_types_compatible(actual: str, declared: str) -> bool:
    if declared in GENERIC_MIME_TYPES or actual == declared:
        return True
    # Telegram и браузеры часто присылают контейнер вместо уточнённого типа.
    if actual in ZIP_CONTAINER_MIME_TYPES and declared == "application/zip":
        return True
    if declared in ZIP_CONTAINER_MIME_TYPES and actual == "application/zip":
        return True
    if actual in OLE_MIME_TYPES and declared in OLE_MIME_TYPES:
        return True
    return actual.startswith("text/") and declared.startswith("text/")


def prepare_upload(
    file_bytes: bytes,
    filename: str | None,
    content_type: str | None = None,
    *,
    max_size: int | None = None,
) -> PreparedUpload:
    """Проверяет размер, имя, MIME по magic bytes и расширение файла."""
    data = bytes(file_bytes or b"")
    if not data:
        raise FileValidationError("Файл пуст.")

    limit = max_upload_bytes() if max_size is None else max_size
    if not check_size_limit(data, limit):
        raise FileTooLarge(f"Файл больше допустимого размера ({limit} байт).")

    clean_name = sanitize_filename(filename)
    actual_mime = detect_mime_type(data, clean_name)
    declared_mime = normalize_mime_type(content_type)
    if not _mime_types_compatible(actual_mime, declared_mime):
        raise FileMimeMismatch(
            f"MIME-тип не совпадает с содержимым: заявлен {declared_mime}, "
            f"фактически {actual_mime}."
        )
    if not validate_extension(actual_mime, clean_name):
        raise FileExtensionMismatch(
            f"Расширение файла «{clean_name}» не соответствует типу {actual_mime}."
        )
    return PreparedUpload(
        filename=clean_name,
        mime_type=actual_mime,
        sha256=calculate_sha256(data),
        size=len(data),
    )


# ---------------------------------------------------------------------------
# Сканирование (ClamAV) и журнал FileScanResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanOutcome:
    """Результат антивирусной проверки для записи в FileScanResult."""

    status: ScanStatus
    scanner: str
    details: str | None = None


async def scan_content(file_bytes: bytes) -> ScanOutcome:
    """Проверяет содержимое через ClamAV (если настроен).

    Контракт статусов: нет хоста → SKIPPED (локальные проверки), сканер
    недоступен → ERROR, чисто → CLEAN, найдена сигнатура → INFECTED.
    """
    host = getattr(settings, "clamav_host", None)
    if not host:
        return ScanOutcome(
            ScanStatus.SKIPPED, "local", "ClamAV не настроен: выполнена только локальная проверка"
        )
    port = getattr(settings, "clamav_port", 3310)
    try:
        import pyclamd

        client = pyclamd.ClamdNetworkSocket(host, port)
        scan_result = client.scan_stream(file_bytes)
    except Exception as exc:  # noqa: BLE001 — сканер недоступен, файл останется непроверенным
        return ScanOutcome(ScanStatus.ERROR, "clamav", f"Сканер недоступен: {exc}")
    if scan_result:
        return ScanOutcome(ScanStatus.INFECTED, "clamav", str(scan_result))
    return ScanOutcome(ScanStatus.CLEAN, "clamav", "Угроз не обнаружено")


async def scan_file_bytes(file_bytes: bytes) -> ScanStatus:
    """Совместимая обёртка для versions.py: заражённый файл вызывает ValueError."""
    outcome = await scan_content(file_bytes)
    if outcome.status is ScanStatus.INFECTED:
        raise ValueError(f"Файл заражён: {outcome.details}")
    return outcome.status


async def record_scan_result(
    session: AsyncSession,
    file_id: UUID,
    outcome: ScanOutcome,
) -> FileScanResult:
    """Пишет строку FileScanResult: кто сканировал, итог и подробности."""
    row = FileScanResult(
        file_id=file_id,
        scanner=outcome.scanner[:64],
        status=outcome.status,
        details=outcome.details,
    )
    session.add(row)
    await session.flush()
    return row


async def rescan_stored_file(session: AsyncSession, file_id: UUID) -> ScanOutcome:
    """Повторная проверка хранящегося файла (задание worker или админ-экран).

    Инфицированный файл не удаляется сам — статус обновляется, решение
    принимает оператор. Результат пишется в FileScanResult.
    """
    file = await session.get(File, file_id)
    if file is None:
        raise FileStateError("Файл не найден.")
    try:
        data = read_object(file.bucket, file.storage_key)
    except Exception as exc:  # noqa: BLE001 — хранилище недоступно
        outcome = ScanOutcome(ScanStatus.ERROR, "local", f"Не удалось прочитать файл: {exc}")
    else:
        outcome = await scan_content(data)
    file.scan_status = outcome.status
    await record_scan_result(session, file.id, outcome)
    await session.commit()
    return outcome


async def scan_file(file_id: UUID | str, session: AsyncSession) -> FileScanResult:
    """Сканирует файл из хранилища и записывает результат в FileScanResult."""
    try:
        resolved_id = file_id if isinstance(file_id, UUID) else UUID(str(file_id))
    except (TypeError, ValueError) as exc:
        raise FileRecordNotFound("Файл не найден.") from exc
    file = await session.get(File, resolved_id)
    if file is None:
        raise FileRecordNotFound("Файл не найден.")

    try:
        data = await asyncio.to_thread(read_object, file.bucket, file.storage_key)
    except Exception as exc:  # noqa: BLE001 — хранилище недоступно
        outcome = ScanOutcome(ScanStatus.ERROR, "local", f"Не удалось прочитать файл: {exc}")
    else:
        outcome = await scan_content(data)

    result = FileScanResult(
        file_id=file.id,
        scanner=outcome.scanner[:64],
        status=outcome.status,
        details=outcome.details,
    )
    session.add(result)
    file.scan_status = outcome.status
    await session.commit()
    await session.refresh(result)
    return result


async def release_after_scan(file_id: UUID | str, session: AsyncSession) -> File:
    """Выпускает файл из карантина только после чистой проверки."""
    try:
        resolved_id = file_id if isinstance(file_id, UUID) else UUID(str(file_id))
    except (TypeError, ValueError) as exc:
        raise FileRecordNotFound("Файл не найден.") from exc
    file = await session.get(File, resolved_id)
    if file is None:
        raise FileRecordNotFound("Файл не найден.")
    if file.scan_status is not ScanStatus.CLEAN:
        status_text = getattr(file.scan_status, "value", file.scan_status)
        raise FileScanError(
            f"Файл не прошёл чистую проверку (статус: {status_text})."
        )
    try:
        released = await release_from_quarantine(file.id, session)
    except QuarantineError as exc:
        raise FileScanError(str(exc)) from exc
    if released is None:
        raise FileRecordNotFound("Файл не найден.")
    return released


# ---------------------------------------------------------------------------
# Хранилище
# ---------------------------------------------------------------------------


def store_object(file_bytes: bytes, object_name: str, content_type: str) -> str:
    """Кладёт объект в бакет и возвращает имя бакета."""
    client = get_minio_client()
    bucket = bucket_name()
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    client.put_object(
        bucket,
        object_name,
        BytesIO(file_bytes),
        length=len(file_bytes),
        content_type=content_type,
    )
    return bucket


def read_object(bucket: str, object_name: str) -> bytes:
    client = get_minio_client()
    response = client.get_object(bucket, object_name)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def remove_objects(bucket: str, object_keys: set[str]) -> None:
    """Удаляет объекты из бакета; отсутствие объекта не считается ошибкой."""
    if not object_keys:
        return
    client = get_minio_client()
    for key in object_keys:
        # Объект мог быть удалён ранее — удаление идемпотентно.
        with suppress(Exception):
            client.remove_object(bucket, key)


# ---------------------------------------------------------------------------
# Загрузка
# ---------------------------------------------------------------------------


async def upload_file(
    session: AsyncSession,
    *,
    category_code: str,
    title: str,
    description: str | None,
    file_bytes: bytes,
    filename: str,
    content_type: str,
    created_by_id: UUID,
) -> Record:
    """Принимает файл через карантинный цикл: проверки → карантин → скан → выпуск.

    Дешёвые локальные проверки идут первыми: ``inspect_upload`` (ZIP bomb,
    magic bytes, path traversal) и ``prepare_upload`` (размер, имя, фактический
    MIME, расширение) выполняются до любой записи в хранилище. Файл кладётся
    в карантин, ``scan_content`` проверяет его ClamAV и пишет результат в
    FileScanResult; выпуск из карантина разрешён только чистому скану.
    Заражённый файл остаётся в карантине, вызывая ``FileInfectedError``.
    """
    user = await session.get(User, created_by_id)
    if not user:
        raise FilePermissionError("Пользователь не найден.")
    await PermissionService.require(
        user, PermissionCode.UPLOAD, TARGET_CATEGORY, category_code, session
    )

    data = bytes(file_bytes or b"")
    # Проверка ZIP bomb и заявленного MIME до записи в хранилище.
    inspect_upload(data, filename, content_type)
    # Строгая проверка фактического MIME, расширения и SHA-256.
    prepared = prepare_upload(data, filename, content_type)

    file = await quarantine_file(
        data,
        prepared.filename,
        session,
        uploaded_by=user.id,
        mime_type=prepared.mime_type,
        sha256=prepared.sha256,
    )
    if not isinstance(file, File):
        raise FileStateError("Не удалось создать запись о файле.")

    # Скан выполняется по байтам до выпуска: результат пишется в
    # FileScanResult и обновляет file.scan_status — аудитабельно для файла.
    outcome = await scan_content(data)
    file.scan_status = outcome.status
    await record_scan_result(session, file.id, outcome)

    if outcome.status is ScanStatus.CLEAN:
        file = await release_after_scan(file.id, session)
    else:
        await session.commit()
        if outcome.status is ScanStatus.INFECTED:
            raise FileInfectedError(
                "Файл заблокирован антивирусом и остаётся в карантине."
            )

    record = await create_record(
        session,
        category_code=category_code,
        record_type=RecordType.FILES,
        title=title or prepared.filename,
        description=description,
        created_by=user,
        fields={
            "filename": prepared.filename,
            "file_id": str(file.id),
            "object_name": file.storage_key,
            "bucket": file.bucket,
            "mime_type": file.mime_type,
            "sha256": file.sha256 or prepared.sha256,
            "size": file.size,
            "scan_status": getattr(file.scan_status, "value", str(file.scan_status)),
        },
    )
    await add_attachment(session, record.id, file.id)  # коммитит транзакцию
    await log_file_access(file.id, user.id, "UPLOAD", ACCESS_SUCCESS, session)
    await log_audit(
        session,
        user.id,
        "UPLOAD_FILE",
        "File",
        str(file.id),
        metadata={
            "record_id": str(record.id),
            "category_code": category_code,
            "filename": prepared.filename,
            "mime_type": file.mime_type,
            "sha256": file.sha256 or prepared.sha256,
            "size": file.size,
            "scan_status": getattr(file.scan_status, "value", str(file.scan_status)),
        },
    )
    return record


# ---------------------------------------------------------------------------
# Скачивание
# ---------------------------------------------------------------------------


async def download_file_bytes(
    session: AsyncSession,
    *,
    user: User | None,
    file_id: UUID,
) -> tuple[File, bytes]:
    """Возвращает файл и байты после проверки прав и скана. Каждая попытка пишется в журнал."""
    user_id = getattr(user, "id", None)
    try:
        await PermissionService.require(
            user, PermissionCode.DOWNLOAD, TARGET_FILE, file_id, session
        )
    except PermissionDenied as exc:
        # Для несуществующего файла журнал не пишем: внешний ключ не даст вставить строку
        if exc.reason != DenyReason.FILE_NOT_FOUND:
            await log_file_access(file_id, user_id, "DOWNLOAD", ACCESS_DENIED, session)
        raise

    file = await session.get(File, file_id)
    try:
        data = read_object(file.bucket, file.storage_key)
    except Exception:
        await log_file_access(file_id, user_id, "DOWNLOAD", ACCESS_ERROR, session)
        raise
    await log_file_access(file_id, user_id, "DOWNLOAD", ACCESS_SUCCESS, session)
    return file, data


# ---------------------------------------------------------------------------
# Жизненный цикл: архивирование, удаление, восстановление, очистка
# ---------------------------------------------------------------------------


async def _get_allowed_file(
    session: AsyncSession,
    user: User | None,
    permission: PermissionCode,
    file_id: UUID,
) -> File:
    """Проверяет право (Default Deny) и возвращает строку файла."""
    await PermissionService.require(user, permission, TARGET_FILE, file_id, session)
    file = await session.get(File, file_id)
    if file is None:
        raise PermissionDenied(
            DenyReason.FILE_NOT_FOUND,
            user_id=getattr(user, "id", None),
            object_type=TARGET_FILE,
            object_id=str(file_id),
        )
    return file


async def archive_file(session: AsyncSession, *, user: User | None, file_id: UUID) -> File:
    """Архивирует файл (soft): скрыть из рабочих экранов, данные остаются."""
    file = await _get_allowed_file(session, user, PermissionCode.ARCHIVE, file_id)
    if file.deleted_at is not None:
        raise FileStateError("Файл уже удалён — сначала восстановите его.")
    file.deleted_at = datetime.now(UTC)
    await log_file_access(file_id, user.id, "ARCHIVE", ACCESS_SUCCESS, session)
    return file


async def soft_delete_file(session: AsyncSession, *, user: User | None, file_id: UUID) -> File:
    """Удаляет файл в корзину (soft): скачивание блокируется, объект остаётся."""
    file = await _get_allowed_file(session, user, PermissionCode.DELETE, file_id)
    if file.deleted_at is not None:
        raise FileStateError("Файл уже в корзине.")
    file.deleted_at = datetime.now(UTC)
    await log_file_access(file_id, user.id, "DELETE", ACCESS_SUCCESS, session)
    return file


async def restore_file(session: AsyncSession, *, user: User | None, file_id: UUID) -> File:
    """Восстанавливает файл из корзины/архива (снимает deleted_at)."""
    file = await _get_allowed_file(session, user, PermissionCode.RESTORE, file_id)
    if file.deleted_at is None:
        raise FileStateError("Файл и так активен — восстанавливать нечего.")
    file.deleted_at = None
    await log_file_access(file_id, user.id, "RESTORE", ACCESS_SUCCESS, session)
    return file


async def purge_file(session: AsyncSession, *, user: User | None, file_id: UUID) -> None:
    """Окончательно удаляет файл: объекты из бакета и строки реестра.

    В корзину файл должен попасть сначала (soft delete) — это защита от
    случайной безвозвратной утраты, как и в жизненном цикле записей.
    """
    file = (
        await session.execute(
            select(File)
            .where(File.id == file_id)
            .options(
                # ORM-каскад снимет версии, связи, журнал и сканы только
                # с предзагруженными коллекциями (в async lazy load невозможен).
                selectinload(File.versions),
                selectinload(File.record_links),
                selectinload(File.access_logs),
                selectinload(File.scan_results),
            )
        )
    ).scalar_one_or_none()
    if file is None:
        raise PermissionDenied(
            DenyReason.FILE_NOT_FOUND,
            user_id=getattr(user, "id", None),
            object_type=TARGET_FILE,
            object_id=str(file_id),
        )
    await PermissionService.require(user, PermissionCode.DELETE, TARGET_FILE, file_id, session)
    if file.deleted_at is None:
        raise FileStateError("Сначала удалите файл в корзину — очистка только для удалённых.")

    object_keys = {file.storage_key} | {item.storage_key for item in file.versions}
    bucket = file.bucket
    await session.delete(file)
    await session.commit()
    remove_objects(bucket, object_keys)


async def list_deleted_files(
    session: AsyncSession,
    *,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[File], int]:
    """Страница файлов в корзине и общее их количество (экран архива файлов)."""
    conditions = [File.deleted_at.is_not(None)]
    total = await session.scalar(select(func.count()).select_from(File).where(*conditions))
    result = await session.execute(
        select(File)
        .where(*conditions)
        .order_by(File.deleted_at.desc(), File.id)
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), total or 0


__all__ = [
    "GENERIC_MIME",
    "MAX_FILE_BYTES",
    "FileError",
    "FileExtensionMismatch",
    "FileInfectedError",
    "FileMimeMismatch",
    "FilePermissionError",
    "FileRecordNotFound",
    "FileScanError",
    "FileSecurityError",
    "FileStateError",
    "FileTooLarge",
    "FileTooLargeError",
    "FileValidationError",
    "PreparedUpload",
    "ScanOutcome",
    "UploadInspection",
    "archive_file",
    "bucket_name",
    "download_file_bytes",
    "get_minio_client",
    "inspect_upload",
    "list_deleted_files",
    "max_upload_bytes",
    "prepare_upload",
    "purge_file",
    "read_object",
    "record_scan_result",
    "release_after_scan",
    "remove_objects",
    "rescan_stored_file",
    "restore_file",
    "scan_content",
    "scan_file",
    "scan_file_bytes",
    "soft_delete_file",
    "store_object",
    "upload_file",
]
