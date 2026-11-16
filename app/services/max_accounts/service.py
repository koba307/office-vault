"""Сервис выдачи MAX-аккаунтов: загрузка txt/zip, однократная выдача."""

from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import File, MaxAccountIssue, ScanStatus, User
from app.services.files.service import read_object, store_object

STORAGE_PREFIX = "max_accounts/"


def _is_txt(name: str) -> bool:
    return name.lower().endswith(".txt")


def _extract_accounts(data: bytes, filename: str) -> list[tuple[str, bytes]]:
    """Возвращает список (имя, содержимое) для .txt или архива с .txt."""
    if _is_txt(filename):
        return [(filename, data)]
    if filename.lower().endswith(".zip"):
        out: list[tuple[str, bytes]] = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir() or not _is_txt(info.filename):
                    continue
                # Защита от path traversal: берём только имя файла.
                safe_name = info.filename.rsplit("/", 1)[-1]
                out.append((safe_name, archive.read(info)))
        return out
    raise ValueError("Поддерживаются только .txt или .zip с .txt файлами.")


async def upload_max_accounts(
    session: AsyncSession,
    user: User,
    data: bytes,
    filename: str,
) -> int:
    """Загружает MAX-аккаунты из .txt или .zip и возвращает количество добавленных."""
    accounts = _extract_accounts(data, filename)
    if not accounts:
        raise ValueError("В файле/архиве нет .txt файлов.")
    count = 0
    for name, payload in accounts:
        storage_key = f"{STORAGE_PREFIX}{uuid4()}.txt"
        bucket = await asyncio.to_thread(store_object, payload, storage_key, "text/plain")
        file = File(
            id=uuid4(),
            original_name=name,
            storage_key=storage_key,
            bucket=bucket,
            mime_type="text/plain",
            extension="txt",
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            scan_status=ScanStatus.CLEAN,
            uploaded_by=user.id,
        )
        session.add(file)
        count += 1
    await session.commit()
    return count


async def remaining_max_accounts(session: AsyncSession) -> int:
    """Количество ещё не выданных MAX-аккаунтов."""
    issued_exists = exists().where(MaxAccountIssue.file_id == File.id)
    result = await session.scalar(
        select(func.count())
        .select_from(File)
        .where(
            File.storage_key.like(f"{STORAGE_PREFIX}%"),
            File.deleted_at.is_(None),
            ~issued_exists,
        )
    )
    return int(result or 0)


async def _candidates(session: AsyncSession, limit: int = 10) -> list[File]:
    issued_exists = exists().where(MaxAccountIssue.file_id == File.id)
    result = await session.execute(
        select(File)
        .where(
            File.storage_key.like(f"{STORAGE_PREFIX}%"),
            File.deleted_at.is_(None),
            ~issued_exists,
        )
        .order_by(File.created_at)
        .limit(limit)
    )
    return list(result.scalars().all())


async def reserve_max_account(
    session: AsyncSession,
    user: User,
) -> tuple[File, bytes] | None:
    """Резервирует один аккаунт за пользователем и возвращает его данные.

    Резерв создаётся до отправки, чтобы конкурентные запросы не выдали
    один и тот же аккаунт дважды. Если прочитать файл не удалось, резерв
    снимается и пробуется следующий кандидат.
    """
    for file in await _candidates(session):
        issue = MaxAccountIssue(id=uuid4(), file_id=file.id, user_id=user.id)
        session.add(issue)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            continue
        try:
            payload = await asyncio.to_thread(read_object, file.bucket, file.storage_key)
        except Exception:  # noqa: BLE001 — битый объект пробуем пропустить
            await session.delete(issue)
            await session.commit()
            continue
        return file, payload
    return None


async def confirm_issue(session: AsyncSession, file_id) -> None:
    """Помечает файл выданным после успешной отправки пользователю."""
    file = await session.get(File, file_id)
    if file is not None and file.deleted_at is None:
        file.deleted_at = datetime.now(UTC)
        await session.commit()


async def release_reservation(session: AsyncSession, file_id) -> None:
    """Снимает резерв, если отправить файл не удалось."""
    result = await session.execute(
        select(MaxAccountIssue).where(MaxAccountIssue.file_id == file_id)
    )
    issue = result.scalar_one_or_none()
    if issue is not None:
        await session.delete(issue)
        await session.commit()
