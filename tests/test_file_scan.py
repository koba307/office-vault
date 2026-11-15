"""Тесты антивирусного сканирования файлов Phase 06.

Проверяют интеграцию ClamAV (scan_content / scan_file_bytes), запись
результатов в FileScanResult (record_scan_result, scan_file,
rescan_stored_file) и поведение upload_file при инфицированном содержимом.
Хранилище и база не требуются: используются заглушки модуля pyclamd и
псевдо-сессии, как в тестах версий и карантина.
"""

import sys
import types
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.database.models import File, ScanStatus, User, UserStatus
from app.services.files import service
from app.services.files.security import ZipBombResult

# ---------------------------------------------------------------------------
# Заглушки
# ---------------------------------------------------------------------------


class _FakeSession:
    """Минимальная имитация AsyncSession: get/add/flush/commit/refresh."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.added = []
        self.commits = 0
        self.flushes = 0
        self.refreshed = []

    async def get(self, model, key):
        return self.objects.get((model, key))

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        self.added.append(obj)
        self.objects[(type(obj), obj.id)] = obj

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        self.refreshed.append(obj)


def _file(**overrides) -> File:
    values = {
        "id": uuid4(),
        "original_name": "акт.pdf",
        "storage_key": "files/uuid/акт.pdf",
        "bucket": "office-vault",
        "mime_type": "application/pdf",
        "extension": "pdf",
        "size": 10,
        "sha256": "a" * 64,
        "scan_status": ScanStatus.PENDING,
        "uploaded_by": uuid4(),
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "deleted_at": None,
    }
    values.update(overrides)
    return File(**values)


def _active_user() -> User:
    return User(
        id=uuid4(),
        telegram_id=123456789,
        username="scanner",
        display_name="Сканер",
        role_id=uuid4(),
        status=UserStatus.ACTIVE,
    )


def _fake_pyclamd(monkeypatch, scan_result=None, error=None):
    """Подменяет pyclamd: scan_stream возвращает результат или бросает ошибку."""

    class ClamdNetworkSocket:
        def __init__(self, host, port):
            self.host, self.port = host, port

        def scan_stream(self, _data):
            if error is not None:
                raise error
            return scan_result

    module = types.ModuleType("pyclamd")
    module.ClamdNetworkSocket = ClamdNetworkSocket
    monkeypatch.setitem(sys.modules, "pyclamd", module)


# ---------------------------------------------------------------------------
# scan_content: контракт статусов ClamAV
# ---------------------------------------------------------------------------


async def test_scan_content_skipped_without_clamav_host(monkeypatch):
    monkeypatch.setattr(service.settings, "clamav_host", None)
    outcome = await service.scan_content(b"data")
    assert outcome.status is ScanStatus.SKIPPED
    assert outcome.scanner == "local"
    assert "ClamAV" in (outcome.details or "")


async def test_scan_content_clean_via_clamav(monkeypatch):
    monkeypatch.setattr(service.settings, "clamav_host", "clamav")
    _fake_pyclamd(monkeypatch, scan_result=None)
    outcome = await service.scan_content(b"data")
    assert outcome.status is ScanStatus.CLEAN
    assert outcome.scanner == "clamav"


async def test_scan_content_infected_keeps_signature_details(monkeypatch):
    monkeypatch.setattr(service.settings, "clamav_host", "clamav")
    _fake_pyclamd(monkeypatch, scan_result={"stream": ("FOUND", "Eicar-Signature")})
    outcome = await service.scan_content(b"data")
    assert outcome.status is ScanStatus.INFECTED
    assert outcome.scanner == "clamav"
    assert "Eicar-Signature" in (outcome.details or "")


async def test_scan_content_error_when_clamav_unavailable(monkeypatch):
    monkeypatch.setattr(service.settings, "clamav_host", "clamav")
    _fake_pyclamd(monkeypatch, error=ConnectionError("no clamd"))
    outcome = await service.scan_content(b"data")
    assert outcome.status is ScanStatus.ERROR
    assert "недоступен" in (outcome.details or "")


async def test_scan_file_bytes_wrapper_contract_preserved(monkeypatch):
    # Совместимость с versions.py: тот же контракт статусов и ValueError при FOUND.
    monkeypatch.setattr(service.settings, "clamav_host", None)
    assert await service.scan_file_bytes(b"data") == ScanStatus.SKIPPED

    monkeypatch.setattr(service.settings, "clamav_host", "clamav")
    _fake_pyclamd(monkeypatch, scan_result={"stream": ("FOUND", "Eicar")})
    with pytest.raises(ValueError, match="заражён"):
        await service.scan_file_bytes(b"data")


# ---------------------------------------------------------------------------
# record_scan_result: строка FileScanResult
# ---------------------------------------------------------------------------


async def test_record_scan_result_writes_row_without_commit():
    session = _FakeSession()
    file_id = uuid4()
    outcome = service.ScanOutcome(ScanStatus.CLEAN, "clamav", "Угроз не обнаружено")

    row = await service.record_scan_result(session, file_id, outcome)

    assert row.file_id == file_id
    assert row.scanner == "clamav"
    assert row.status is ScanStatus.CLEAN
    assert row.details == "Угроз не обнаружено"
    assert session.added == [row]
    assert session.flushes == 1
    assert session.commits == 0  # коммит остаётся вызывающему коду


# ---------------------------------------------------------------------------
# scan_file / rescan_stored_file: перескан из хранилища
# ---------------------------------------------------------------------------


async def test_scan_file_reads_storage_and_updates_status(monkeypatch):
    monkeypatch.setattr(service.settings, "clamav_host", None)
    file = _file(scan_status=ScanStatus.PENDING)
    session = _FakeSession({(File, file.id): file})
    monkeypatch.setattr(service, "read_object", lambda bucket, key: b"%PDF content")

    result = await service.scan_file(file.id, session)

    assert result.file_id == file.id
    assert result.status is ScanStatus.SKIPPED
    assert file.scan_status is ScanStatus.SKIPPED
    assert session.commits == 1
    assert result in session.refreshed


async def test_scan_file_records_infected_result(monkeypatch):
    monkeypatch.setattr(service.settings, "clamav_host", "clamav")
    _fake_pyclamd(monkeypatch, scan_result={"stream": ("FOUND", "Eicar-Signature")})
    file = _file(scan_status=ScanStatus.PENDING)
    session = _FakeSession({(File, file.id): file})
    monkeypatch.setattr(service, "read_object", lambda bucket, key: b"malicious")

    result = await service.scan_file(file.id, session)

    assert result.status is ScanStatus.INFECTED
    assert result.scanner == "clamav"
    assert "Eicar" in result.details
    assert file.scan_status is ScanStatus.INFECTED
    # Инфицированный файл не выпускается из карантина автоматически.
    assert result in session.added


async def test_scan_file_storage_error_becomes_error_status(monkeypatch):
    file = _file()
    session = _FakeSession({(File, file.id): file})

    def broken(_bucket, _key):
        raise ConnectionError("minio down")

    monkeypatch.setattr(service, "read_object", broken)

    result = await service.scan_file(file.id, session)
    assert result.status is ScanStatus.ERROR
    assert file.scan_status is ScanStatus.ERROR


async def test_scan_file_rejects_unknown_id():
    with pytest.raises(service.FileRecordNotFound):
        await service.scan_file("not-a-uuid", _FakeSession())
    with pytest.raises(service.FileRecordNotFound):
        await service.scan_file(uuid4(), _FakeSession())


async def test_rescan_stored_file_writes_scan_row_and_status(monkeypatch):
    monkeypatch.setattr(service.settings, "clamav_host", "clamav")
    _fake_pyclamd(monkeypatch, scan_result=None)
    file = _file(scan_status=ScanStatus.PENDING)
    session = _FakeSession({(File, file.id): file})
    monkeypatch.setattr(service, "read_object", lambda bucket, key: b"data")

    outcome = await service.rescan_stored_file(session, file.id)

    assert outcome.status is ScanStatus.CLEAN
    assert file.scan_status is ScanStatus.CLEAN
    assert session.added and session.added[0].status is ScanStatus.CLEAN
    assert session.commits == 1


async def test_rescan_stored_file_requires_existing_file():
    with pytest.raises(service.FileStateError):
        await service.rescan_stored_file(_FakeSession(), uuid4())


# ---------------------------------------------------------------------------
# upload_file: блокировка инфицированного и запись FileScanResult
# ---------------------------------------------------------------------------


async def _noop_async(*_args, **_kwargs):
    return None


def _patch_upload_seams(monkeypatch, scan_outcome, created_files):
    """Собирает минимальный контур upload_file без БД и хранилища."""

    async def fake_quarantine(file_bytes, filename, session, **kwargs):
        file = _file(original_name=filename, scan_status=ScanStatus.PENDING)
        session.add(file)
        await session.flush()
        created_files.append(file)
        return file

    async def fake_scan_content(_data):
        return scan_outcome

    async def fake_release(file_id, session):
        file = session.objects[(File, file_id)]
        file.storage_key = file.storage_key.replace("files/", "files/released/", 1)
        return file

    async def fake_create_record(_session, **kwargs):
        return types.SimpleNamespace(id=uuid4(), title=kwargs["title"], fields=[])

    monkeypatch.setattr(service.PermissionService, "require", staticmethod(_noop_async))
    monkeypatch.setattr(service, "quarantine_file", fake_quarantine)
    monkeypatch.setattr(service, "scan_content", fake_scan_content)
    monkeypatch.setattr(service, "release_after_scan", fake_release)
    monkeypatch.setattr(service, "create_record", fake_create_record)
    monkeypatch.setattr(service, "add_attachment", _noop_async)
    monkeypatch.setattr(service, "log_file_access", _noop_async)
    monkeypatch.setattr(service, "log_audit", _noop_async)


async def test_upload_file_blocks_infected_and_keeps_audit_row(monkeypatch):
    user = _active_user()
    session = _FakeSession({(User, user.id): user})
    created_files = []
    _patch_upload_seams(
        monkeypatch,
        service.ScanOutcome(ScanStatus.INFECTED, "clamav", "{'stream': ('FOUND', 'Eicar')}"),
        created_files,
    )
    monkeypatch.setattr(
        service,
        "release_after_scan",
        lambda *_a, **_k: pytest.fail("release must not be called for infected file"),
    )

    with pytest.raises(service.FileInfectedError):
        await service.upload_file(
            session,
            category_code="general",
            title="",
            description=None,
            file_bytes=b"%PDF-1.7\ncontent",
            filename="договор.pdf",
            content_type="application/pdf",
            created_by_id=user.id,
        )

    # Файл остался в реестре карантинным, результат скана записан.
    assert created_files and created_files[0].scan_status is ScanStatus.INFECTED
    scan_rows = [item for item in session.added if hasattr(item, "scanner")]
    assert len(scan_rows) == 1
    assert scan_rows[0].status is ScanStatus.INFECTED
    assert scan_rows[0].scanner == "clamav"
    assert session.commits == 1  # аудиторские записи закоммичены до raise


async def test_upload_file_records_clean_scan_row(monkeypatch):
    user = _active_user()
    session = _FakeSession({(User, user.id): user})
    created_files = []
    _patch_upload_seams(
        monkeypatch,
        service.ScanOutcome(ScanStatus.CLEAN, "clamav", "Угроз не обнаружено"),
        created_files,
    )

    record = await service.upload_file(
        session,
        category_code="general",
        title="",
        description=None,
        file_bytes=b"%PDF-1.7\ncontent",
        filename="договор.pdf",
        content_type="application/pdf",
        created_by_id=user.id,
    )

    assert record.title == "договор.pdf"
    scan_rows = [item for item in session.added if hasattr(item, "scanner")]
    assert len(scan_rows) == 1
    assert scan_rows[0].status is ScanStatus.CLEAN
    assert scan_rows[0].scanner == "clamav"
    assert scan_rows[0].file_id == created_files[0].id


async def test_upload_file_security_rejects_bomb_before_quarantine(monkeypatch):
    import io
    import zipfile

    user = _active_user()
    session = _FakeSession({(User, user.id): user})
    monkeypatch.setattr(service.PermissionService, "require", staticmethod(_noop_async))
    monkeypatch.setattr(
        service,
        "quarantine_file",
        lambda *_a, **_k: pytest.fail("quarantine must not be called for unsafe archives"),
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("huge.bin", b"\x00" * (8 * 1024 * 1024))

    with pytest.raises(service.FileSecurityError):
        await service.upload_file(
            session,
            category_code="general",
            title="",
            description=None,
            file_bytes=buffer.getvalue(),
            filename="photos.zip",
            content_type="application/zip",
            created_by_id=user.id,
        )


async def test_upload_file_requires_existing_user(monkeypatch):
    monkeypatch.setattr(
        service,
        "quarantine_file",
        lambda *_a, **_k: pytest.fail("quarantine must not be called"),
    )
    with pytest.raises(service.FilePermissionError):
        await service.upload_file(
            _FakeSession(),
            category_code="general",
            title="",
            description=None,
            file_bytes=b"data",
            filename="file.txt",
            content_type="text/plain",
            created_by_id=uuid4(),
        )


# ---------------------------------------------------------------------------
# Контракт security-модуля, используемый сервисом
# ---------------------------------------------------------------------------


def test_service_uses_security_contract():
    # inspect_upload обязан опираться на три требуемые функции безопасности.
    source = service.__dict__["inspect_upload"].__code__.co_names
    assert "check_zip_bomb" in source
    assert "sanitize_path" in source
    assert "validate_magic_bytes" in source
    assert isinstance(service.check_zip_bomb(b"", 10, 10), ZipBombResult)
