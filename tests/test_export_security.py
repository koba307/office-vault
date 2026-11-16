"""Безопасность экспорта Phase 08: секреты, права, шифрование артефактов, защита путей.

Тесты без базы данных проверяют правила напрямую. Сценарии с реальными правами в БД
выполняются только при заданном TEST_DATABASE_URL.
"""

import io
import os
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.encryption import decrypt_bytes, encrypt_bytes, encrypt_secret
from app.core.permissions import PermissionAction, check_role_permission
from app.database.models import (
    AuditLog,
    Category,
    ExportJob,
    PermissionCode,
    Record,
    RecordField,
    RecordType,
    Role,
    RoleCode,
    User,
    UserStatus,
)
from app.database.seed import PERMISSIONS, ROLE_PERMISSIONS
from app.services.files.security import sanitize_path
from app.services.import_export.export import (
    ExportPermissionError,
    ensure_export_rights,
    export_records,
    secret_export_role_allowed,
)
from app.services.import_export.export_formats import csv_bytes, json_bytes, xlsx_bytes
from app.services.import_export.rows import build_table
from app.services.import_export.zip_export import assemble_zip
from app.services.records.service import create_record, permanent_delete, soft_delete_record

SECRET = "super-secret-Ж-12345"


def _record_with_secret() -> Record:
    category = Category(
        id=uuid4(), code="holodka", name="Холодка", category_type="root", status="ACTIVE"
    )
    record = Record(
        id=uuid4(),
        category=category,
        record_type=RecordType.CREDENTIALS,
        title="Почта",
        status="ACTIVE",
        created_by=uuid4(),
    )
    record.created_at = datetime(2026, 11, 15, tzinfo=UTC)
    record.fields = [
        RecordField(
            field_key="password",
            field_label="Пароль",
            field_type="PASSWORD",
            encrypted_value=encrypt_secret(SECRET),
            sort_order=0,
        )
    ]
    return record


# ---------------------------------------------------------------------------
# Роли и право EXPORT_SECRETS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [RoleCode.SUPERADMIN, RoleCode.BOSS, "BOSS", "superadmin"])
def test_secret_export_allowed_for_superadmin_and_boss(role):
    assert secret_export_role_allowed(role) is True


@pytest.mark.parametrize("role", [RoleCode.EMPLOYEE, "EMPLOYEE", None, "", "ROOT", 42])
def test_secret_export_denied_for_other_roles(role):
    assert secret_export_role_allowed(role) is False


def test_export_secrets_permission_is_registered():
    assert PermissionCode.EXPORT_SECRETS.value == "EXPORT_SECRETS"
    assert PermissionAction.EXPORT_SECRETS.value == "EXPORT_SECRETS"
    assert any(code is PermissionCode.EXPORT_SECRETS for code, _ in PERMISSIONS)


def test_export_secrets_role_matrix():
    assert check_role_permission(RoleCode.SUPERADMIN, PermissionAction.EXPORT_SECRETS)
    assert check_role_permission(RoleCode.BOSS, PermissionAction.EXPORT_SECRETS)
    assert not check_role_permission(RoleCode.EMPLOYEE, PermissionAction.EXPORT_SECRETS)


def test_seed_grants_export_secrets_only_to_boss_and_superadmin():
    boss = ROLE_PERMISSIONS[RoleCode.BOSS.value]
    employee = ROLE_PERMISSIONS[RoleCode.EMPLOYEE.value]

    assert PermissionCode.EXPORT_SECRETS in boss
    assert PermissionCode.EXPORT_SECRETS in ROLE_PERMISSIONS[RoleCode.SUPERADMIN.value]
    assert PermissionCode.EXPORT_SECRETS not in employee


async def test_employee_is_refused_secret_export_before_database_access():
    # Сессия не передаётся: отказ по роли должен случиться до любого обращения к БД.
    employee = SimpleNamespace(id=uuid4(), role=RoleCode.EMPLOYEE, status=UserStatus.ACTIVE)

    with pytest.raises(ExportPermissionError, match="руководителю"):
        await ensure_export_rights(None, employee, category=None, include_secrets=True)


async def test_missing_user_is_refused_export():
    with pytest.raises(ExportPermissionError):
        await ensure_export_rights(None, None, category=None, include_secrets=False)


# ---------------------------------------------------------------------------
# Секреты не попадают в файлы без подтверждения
# ---------------------------------------------------------------------------


def test_masked_secret_absent_from_csv_json_and_xlsx():
    table = build_table([_record_with_secret()], include_secrets=False)

    csv_text = csv_bytes(table).decode("utf-8-sig")
    json_text = json_bytes(table, generated_at=datetime.now(UTC)).decode("utf-8")
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes(table))) as archive:
        xlsx_text = "".join(archive.read(name).decode("utf-8") for name in archive.namelist())

    for text in (csv_text, json_text, xlsx_text):
        assert SECRET not in text
    assert "***" in csv_text


def test_secret_present_in_all_formats_only_when_explicitly_requested():
    table = build_table([_record_with_secret()], include_secrets=True)

    assert SECRET in csv_bytes(table).decode("utf-8-sig")
    assert SECRET in json_bytes(table, generated_at=datetime.now(UTC)).decode("utf-8")


def test_secret_masked_in_zip_records_payload():
    table = build_table([_record_with_secret()], include_secrets=False)

    payload = assemble_zip([("records.json", json_bytes(table, generated_at=datetime.now(UTC)))])

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert SECRET.encode() not in archive.read("records.json")


# ---------------------------------------------------------------------------
# Шифрование артефакта экспорта
# ---------------------------------------------------------------------------


def test_export_artifact_roundtrip_and_ciphertext_hides_content():
    plaintext = f"password={SECRET}".encode()

    blob = encrypt_bytes(plaintext)

    assert SECRET.encode() not in blob
    assert decrypt_bytes(blob) == plaintext


def test_tampered_export_artifact_is_rejected():
    blob = bytearray(encrypt_bytes("данные экспорта".encode()))
    blob[-1] ^= 0x01

    with pytest.raises(InvalidTag):
        decrypt_bytes(bytes(blob))


def test_two_encryptions_of_same_content_differ():
    assert encrypt_bytes(b"same") != encrypt_bytes(b"same")


# ---------------------------------------------------------------------------
# Защита имён и путей
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    ["../../etc/passwd", "..\\..\\boot.ini", "/root/.ssh/id_rsa", "a/../../b.txt"],
)
def test_attachment_names_cannot_escape_archive(hostile):
    safe = sanitize_path(hostile)

    assert "/" not in safe
    assert "\\" not in safe
    assert safe not in ("", ".", "..")


def test_zip_rejects_path_traversal_members():
    with pytest.raises(ValueError):
        assemble_zip([("files/../../escape.txt", b"x")])


# ---------------------------------------------------------------------------
# SQL задач: очистка по сроку использует корректный JSONB-доступ
# ---------------------------------------------------------------------------


def test_purge_query_uses_jsonb_text_access():
    condition = ExportJob.filters["purged_at"].astext.is_(None)

    sql = str(condition.compile(dialect=postgresql.dialect()))

    assert "->>" in sql
    assert "IS NULL" in sql.upper()


# ---------------------------------------------------------------------------
# Интеграционные сценарии с правами в БД (нужен TEST_DATABASE_URL)
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_session():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated PostgreSQL test database")

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as session:
            try:
                await session.execute(select(Role.id).limit(1))
            except SQLAlchemyError:
                pytest.skip("Apply migrations and seed the test database first")
            yield session
    finally:
        await engine.dispose()


async def test_employee_export_with_secrets_is_denied_and_audited(db_session):
    employee = await _make_user(db_session, RoleCode.EMPLOYEE)

    with pytest.raises(ExportPermissionError):
        await export_records(db_session, employee, format="csv", include_secrets=True)

    denied = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.actor_user_id == employee.id,
                    AuditLog.action == "EXPORT_SECRETS",
                    AuditLog.result == "DENIED",
                )
            )
        )
        .scalars()
        .all()
    )
    assert denied


async def _make_user(session, role: RoleCode) -> User:
    """Свой активный пользователь нужной роли: тесты не зависят от состояния системного админа."""
    role_row = (
        await session.execute(select(Role).where(Role.code == role.value))
    ).scalar_one_or_none()
    if role_row is None:
        pytest.skip(f"Role {role.value} is not seeded")
    user = User(
        telegram_id=uuid4().int % 10**12,
        role=role,
        role_id=role_row.id,
        status=UserStatus.ACTIVE,
    )
    session.add(user)
    await session.commit()
    return user


async def test_superadmin_secret_export_end_to_end(db_session, monkeypatch):
    from app.services.import_export import export_jobs

    storage: dict[str, bytes] = {}

    def fake_store(payload, key, content_type):
        storage[key] = payload
        return "test-bucket"

    def fake_remove(bucket, keys):
        for key in keys:
            storage.pop(key, None)

    monkeypatch.setattr(export_jobs, "store_object", fake_store)
    monkeypatch.setattr(export_jobs, "remove_objects", fake_remove)

    admin = await _make_user(db_session, RoleCode.SUPERADMIN)
    record = await create_record(
        db_session,
        category_code="holodka",
        record_type=RecordType.CREDENTIALS,
        title=f"Экспорт {uuid4().hex[:8]}",
        description=None,
        created_by=admin,
        fields={"login": "qa-user", "password": SECRET},
    )
    try:
        job = await export_jobs.create_export_job(
            db_session, admin, "holodka", "csv", record_ids=[record.id], include_secrets=True
        )
        artifact = await export_jobs.run_export_job(db_session, job.id, user=admin)

        assert SECRET in artifact.data.decode("utf-8-sig")
        assert artifact.include_secrets is True
        assert artifact.record_count == 1
        stored = [key for key in storage if key.startswith("exports/")]
        assert stored
        assert all(SECRET.encode() not in storage[key] for key in stored)

        status = await export_jobs.get_export_status(db_session, job.id, user=admin)
        assert status["status"] == "COMPLETED"
        assert status["available"] is True

        purged = await export_jobs.purge_expired_exports(
            db_session, now=datetime.now(UTC) + timedelta(days=2)
        )
        assert purged >= 1
        assert not any(key == stored[0] for key in storage)
        status = await export_jobs.get_export_status(db_session, job.id, user=admin)
        assert status["expired"] is True
        assert status["available"] is False

        audit = (
            (
                await db_session.execute(
                    select(AuditLog).where(
                        AuditLog.actor_user_id == admin.id,
                        AuditLog.action == "EXPORT_SECRETS",
                        AuditLog.result == "SUCCESS",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert audit
        assert any(str(record.id) in (item.metadata_ or {}).get("record_ids", []) for item in audit)
    finally:
        await soft_delete_record(db_session, record.id, admin.id)
        await permanent_delete(db_session, record.id, admin.id)


async def test_export_job_of_another_user_is_hidden(db_session, monkeypatch):
    from app.services.import_export import export_jobs

    admin = await _make_user(db_session, RoleCode.SUPERADMIN)
    job = await export_jobs.create_export_job(db_session, admin, "holodka", "json")
    outsider = SimpleNamespace(id=uuid4(), role=RoleCode.EMPLOYEE)

    assert await export_jobs.get_export_status(db_session, job.id, user=outsider) is None


async def test_storage_failure_marks_job_failed_with_safe_message(db_session, monkeypatch):
    from app.services.import_export import export_jobs

    def broken_store(payload, key, content_type):
        raise OSError("connection refused: minio.internal:9000 secret-key-123")

    monkeypatch.setattr(export_jobs, "store_object", broken_store)
    admin = await _make_user(db_session, RoleCode.SUPERADMIN)
    job = await export_jobs.create_export_job(db_session, admin, "holodka", "json")

    with pytest.raises(export_jobs.ExportJobError):
        await export_jobs.run_export_job(db_session, job.id, user=admin)

    status = await export_jobs.get_export_status(db_session, job.id, user=admin)
    assert status["status"] == "FAILED"
    assert status["error"] == "Хранилище экспорта недоступно. Попробуйте позже."
    assert "secret-key-123" not in str(status)


async def test_completed_job_cannot_run_twice(db_session, monkeypatch):
    from app.services.import_export import export_jobs

    monkeypatch.setattr(export_jobs, "store_object", lambda payload, key, ct: "test-bucket")
    admin = await _make_user(db_session, RoleCode.SUPERADMIN)
    job = await export_jobs.create_export_job(db_session, admin, "holodka", "json")
    await export_jobs.run_export_job(db_session, job.id, user=admin)

    with pytest.raises(export_jobs.ExportJobError, match="уже выполнена"):
        await export_jobs.run_export_job(db_session, job.id, user=admin)
