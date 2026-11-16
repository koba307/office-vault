"""Сервис резервного копирования PostgreSQL/конфигурации."""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database.models import Backup, BackupKind, JobStatus
from app.services.files.service import get_minio_client, store_object


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.master_encryption_key.encode()).digest())
    return Fernet(key)


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


async def create_backup(
    session: AsyncSession,
    *,
    kind: BackupKind = BackupKind.FULL,
    include_config: bool = True,
) -> Backup:
    """Создаёт зашифрованную резервную копию и сохраняет её в хранилище."""
    backup = Backup(
        id=uuid4(),
        kind=kind,
        status=JobStatus.PROCESSING,
        encrypted=True,
    )
    session.add(backup)
    await session.commit()

    tmpdir = Path(tempfile.mkdtemp(prefix="office_vault_backup_"))
    try:
        dump_path = tmpdir / "database.sql"
        # PostgreSQL dump
        env = os.environ.copy()
        env["PGPASSWORD"] = settings.database_url.split(":", 3)[-1].split("@")[0] if ":" in settings.database_url else ""
        # Парсим пароль из DATABASE_URL
        db_url = settings.database_url
        # postgresql+asyncpg://user:pass@host:port/db
        rest = db_url.split("://", 1)[1]
        creds, hostpart = rest.split("@", 1)
        user, password = creds.split(":", 1)
        hostport, dbname = hostpart.split("/", 1)
        if ":" in hostport:
            host, port = hostport.split(":", 1)
        else:
            host, port = hostport, "5432"
        env["PGPASSWORD"] = password

        with open(dump_path, "wb") as f:
            subprocess.run(
                ["pg_dump", "-h", host, "-p", port, "-U", user, "-d", dbname, "-F", "p"],
                stdout=f,
                check=True,
                env=env,
            )

        # Архив
        archive_path = tmpdir / "backup.tar.gz"
        with open(archive_path, "wb") as f:
            # tar database.sql (+ config)
            members = ["database.sql"]
            if include_config:
                cfg = tmpdir / "config.env"
                cfg.write_text("APP=office-vault\n", encoding="utf-8")
                members.append("config.env")
            subprocess.run(
                ["tar", "-czf", str(archive_path), "-C", str(tmpdir), *members],
                check=True,
            )

        raw = archive_path.read_bytes()
        encrypted = _fernet().encrypt(raw)
        storage_key = f"backups/{backup.id}.enc"
        bucket = store_object(encrypted, storage_key, "application/octet-stream")

        sha = hashlib.sha256(encrypted).hexdigest()
        backup.storage_key = storage_key
        backup.size = len(encrypted)
        backup.sha256 = sha
        backup.status = JobStatus.COMPLETED
        backup.finished_at = datetime.now(UTC)
        # bucket stored in storage_key prefix? keep key only
        await session.commit()

        await _prune_old_backups(session)
        return backup
    except Exception as exc:  # noqa: BLE001
        backup.status = JobStatus.FAILED
        backup.finished_at = datetime.now(UTC)
        await session.commit()
        raise RuntimeError(f"Backup failed: {exc}") from exc
    finally:
        for p in tmpdir.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            tmpdir.rmdir()
        except OSError:
            pass


async def _prune_old_backups(session: AsyncSession, keep: int = 5) -> None:
    """Оставляет только ``keep`` последних успешных бэкапов."""
    result = await session.execute(
        select(Backup).where(Backup.status == JobStatus.COMPLETED).order_by(Backup.created_at.desc())
    )
    backups = result.scalars().all()
    for old in backups[keep:]:
        try:
            from app.services.files.service import remove_objects

            if old.storage_key:
                remove_objects("office-vault", {old.storage_key})
        except Exception:  # noqa: BLE001
            pass
        await session.delete(old)
    await session.commit()


async def list_backups(session: AsyncSession, *, limit: int = 20) -> list[Backup]:
    result = await session.execute(
        select(Backup).order_by(Backup.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def verify_backup(session: AsyncSession, backup_id: UUID | str) -> bool:
    """Проверяет целостность бэкапа по SHA-256."""
    backup = await session.get(Backup, UUID(str(backup_id)))
    if backup is None or not backup.storage_key or not backup.sha256:
        return False
    try:
        from app.services.files.service import get_minio_client

        client = get_minio_client()
        response = client.get_object("office-vault", backup.storage_key)
        try:
            data = response.read()
        finally:
            response.close()
            response.release_conn()
        return hashlib.sha256(data).hexdigest() == backup.sha256
    except Exception:  # noqa: BLE001
        return False


async def restore_backup(session: AsyncSession, backup_id: UUID | str) -> str:
    """Восстанавливает БД из бэкапа (осторожно: перезаписывает данные)."""
    backup = await session.get(Backup, UUID(str(backup_id)))
    if backup is None or not backup.storage_key:
        raise ValueError("Бэкап не найден.")
    if not await verify_backup(session, backup.id):
        raise ValueError("Контрольная сумма бэкапа не совпадает.")

    from app.services.files.service import get_minio_client

    client = get_minio_client()
    response = client.get_object("office-vault", backup.storage_key)
    try:
        encrypted = response.read()
    finally:
        response.close()
        response.release_conn()

    raw = _fernet().decrypt(encrypted)
    tmpdir = Path(tempfile.mkdtemp(prefix="office_vault_restore_"))
    try:
        archive = tmpdir / "backup.tar.gz"
        archive.write_bytes(raw)
        subprocess.run(["tar", "-xzf", str(archive), "-C", str(tmpdir)], check=True)
        dump = tmpdir / "database.sql"
        if not dump.exists():
            raise ValueError("В архиве нет database.sql.")

        db_url = settings.database_url
        rest = db_url.split("://", 1)[1]
        creds, hostpart = rest.split("@", 1)
        user, password = creds.split(":", 1)
        hostport, dbname = hostpart.split("/", 1)
        host, port = (hostport.split(":", 1) + ["5432"])[:2]

        env = os.environ.copy()
        env["PGPASSWORD"] = password
        with open(dump, "rb") as f:
            subprocess.run(
                ["psql", "-h", host, "-p", port, "-U", user, "-d", dbname],
                stdin=f,
                check=True,
                env=env,
            )
        return f"Восстановление из бэкапа {backup.id} выполнено."
    finally:
        for p in tmpdir.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            tmpdir.rmdir()
        except OSError:
            pass
