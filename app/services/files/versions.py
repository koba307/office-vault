"""Версии файлов: снимки содержимого, восстановление и замена.

Текущее содержимое хранится в строке `files`. Строка в `file_versions` — снимок содержимого,
которое было актуальным до замены или восстановления. Номер текущей версии на единицу больше
последнего сохранённого. Объекты в хранилище не удаляются, поэтому восстановление не требует
повторной загрузки байтов. Метаданные (имя, MIME, расширение) при замене не меняются.
"""

from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import File, FileVersion
from app.services.files.service import scan_file_bytes, store_object


class FileVersionError(ValueError):
    """Базовая ошибка домена версий файлов."""


class FileNotFound(FileVersionError):
    """Файл не найден или удалён."""


class FileVersionNotFound(FileVersionError):
    """Снимок указанной версии не найден."""


async def _load_file(session: AsyncSession, file_id: UUID) -> File:
    file = await session.get(File, file_id)
    if file is None or file.deleted_at is not None:
        raise FileNotFound("Файл не найден.")
    return file


async def current_version_number(file_id: UUID, session: AsyncSession) -> int:
    last = await session.scalar(
        select(func.max(FileVersion.version)).where(FileVersion.file_id == file_id)
    )
    return (last or 0) + 1


def _snapshot(file: File, version: int) -> FileVersion:
    return FileVersion(
        file_id=file.id,
        version=version,
        storage_key=file.storage_key,
        size=file.size,
        sha256=file.sha256,
        scan_status=file.scan_status,
    )


async def create_file_version(file_id: UUID, session: AsyncSession) -> FileVersion:
    """Сохраняет снимок текущего содержимого под номером текущей версии."""
    file = await _load_file(session, file_id)
    row = _snapshot(file, await current_version_number(file_id, session))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def list_file_versions(file_id: UUID, session: AsyncSession) -> list[FileVersion]:
    """Сохранённые снимки файла, от новых к старым. Текущая версия в список не входит."""
    result = await session.execute(
        select(FileVersion)
        .where(FileVersion.file_id == file_id)
        .order_by(FileVersion.version.desc())
    )
    return list(result.scalars().all())


async def restore_file_version(file_id: UUID, version: int, session: AsyncSession) -> File:
    """Делает содержимое версии текущим. Прежнее содержимое сохраняется как новый снимок."""
    file = await _load_file(session, file_id)
    target = await session.scalar(
        select(FileVersion).where(FileVersion.file_id == file_id, FileVersion.version == version)
    )
    if target is None:
        raise FileVersionNotFound(f"Версия {version} не найдена.")

    session.add(_snapshot(file, await current_version_number(file_id, session)))
    file.storage_key = target.storage_key
    file.size = target.size
    file.sha256 = target.sha256
    file.scan_status = target.scan_status
    await session.commit()
    await session.refresh(file)
    return file


async def replace_file(file_id: UUID, new_bytes: bytes, session: AsyncSession) -> File:
    """Заменяет содержимое файла новыми байтами. Прежнее содержимое становится версией.

    Новые байты проходят скан до загрузки в хранилище; заражённый файл вызывает ValueError.
    """
    file = await _load_file(session, file_id)
    scan_status = await scan_file_bytes(new_bytes)

    object_name = f"attachments/{uuid4()}/{file.original_name.replace('/', '_') or 'file'}"
    bucket = store_object(new_bytes, object_name, file.mime_type)

    session.add(_snapshot(file, await current_version_number(file_id, session)))
    file.storage_key = object_name
    file.bucket = bucket
    file.size = len(new_bytes)
    file.sha256 = sha256(new_bytes).hexdigest()
    file.scan_status = scan_status
    await session.commit()
    await session.refresh(file)
    return file
