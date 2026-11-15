"""Тесты карантина файлов Phase 06 и полного цикла загрузки."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.database.models import (
    File,
    ScanStatus,
    User,
    UserStatus,
)
from app.services.files import quarantine, service
from app.services.files.quarantine import (
    QUARANTINE_PREFIX,
    RELEASED_PREFIX,
    QuarantineError,
    build_storage_key,
    is_quarantine_key,
    is_quarantined,
    quarantine_file,
    release_from_quarantine,
)
from app.services.files.service import (
    FileRecordNotFound,
    FileScanError,
    release_after_scan,
    scan_file,
)

# ---------------------------------------------------------------------------
# Заглушки хранилища и сессии
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data
        self.closed = False
        self.released = False

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class _FakeMinio:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.buckets: set[str] = set()

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.buckets

    def make_bucket(self, bucket: str) -> None:
        self.buckets.add(bucket)

    def put_object(self, bucket, key, data, length, content_type=None):
        self.buckets.add(bucket)
        self.objects[(bucket, key)] = data.read(length)

    def get_object(self, bucket, key):
        return _FakeResponse(self.objects[(bucket, key)])

    def copy_object(self, bucket, key, source):
        self.objects[(bucket, key)] = self.objects[(source.bucket_name, source.object_name)]

    def remove_object(self, bucket, key):
        self.objects.pop((bucket, key), None)


class _FakeSession:
    """Минимальная сессия: get по (модель, ключ), add/flush/commit."""

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
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()
            self.objects[(type(obj), obj.id)] = obj

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        self.refreshed.append(obj)


def _file(*, scan_status=ScanStatus.PENDING, storage_key="quarantine/x/file.pdf", **overrides):
    values = {
        "id": uuid4(),
        "original_name": "file.pdf",
        "storage_key": storage_key,
        "bucket": "office-vault",
        "mime_type": "application/pdf",
        "size": 10,
        "sha256": "f" * 64,
        "scan_status": scan_status,
        "uploaded_by": uuid4(),
        "deleted_at": None,
    }
    values.update(overrides)
    return File(**values)


def _session_with(file):
    return _FakeSession({(File, file.id): file})


@pytest.fixture
def storage(monkeypatch):
    client = _FakeMinio()
    monkeypatch.setattr(quarantine, "get_minio_client", lambda: client)
    return client


def _put(storage, file):
    storage.buckets.add(file.bucket)
    storage.objects[(file.bucket, file.storage_key)] = b"file-bytes"


# ---------------------------------------------------------------------------
# Ключи и префиксы
# ---------------------------------------------------------------------------


def test_build_storage_key_uses_prefix_and_safe_name():
    key = build_storage_key(QUARANTINE_PREFIX, "../../договор.pdf")
    assert key.startswith(QUARANTINE_PREFIX)
    assert key.endswith("/договор.pdf")
    assert ".." not in key
    assert len(key) <= 1024


def test_is_quarantine_key():
    assert is_quarantine_key("quarantine/abc/file.pdf") is True
    assert is_quarantine_key("/quarantine/abc/file.pdf") is True
    assert is_quarantine_key("files/abc/file.pdf") is False
    assert is_quarantine_key(None) is False


# ---------------------------------------------------------------------------
# Помещение в карантин
# ---------------------------------------------------------------------------


async def test_quarantine_file_without_db_returns_entry(storage):
    entry = await quarantine_file(b"%PDF-1.7\ncontent", "../../договор.pdf", _FakeSession())

    assert entry.original_name == "договор.pdf"
    assert entry.mime_type == "application/pdf"
    assert entry.storage_key.startswith(QUARANTINE_PREFIX)
    assert entry.bucket == "office-vault"
    assert len(entry.sha256) == 64
    assert storage.objects[(entry.bucket, entry.storage_key)] == b"%PDF-1.7\ncontent"


async def test_quarantine_file_creates_pending_file(storage):
    session = _FakeSession()
    uploader = uuid4()

    file = await quarantine_file(
        b"%PDF-1.7\ncontent",
        "../../договор.pdf",
        session,
        uploaded_by=uploader,
    )

    assert isinstance(file, File)
    assert file.scan_status == ScanStatus.PENDING
    assert file.uploaded_by == uploader
    assert file.original_name == "договор.pdf"
    assert file.extension == "pdf"
    assert file.size == len(b"%PDF-1.7\ncontent")
    assert file.storage_key.startswith(QUARANTINE_PREFIX)
    assert storage.objects[(file.bucket, file.storage_key)] == b"%PDF-1.7\ncontent"
    assert session.flushes == 1


async def test_quarantine_file_keeps_explicit_metadata(storage):
    entry = await quarantine_file(
        b"payload",
        "custom.bin",
        _FakeSession(),
        mime_type="application/pdf",
        sha256="a" * 64,
    )

    assert entry.mime_type == "application/pdf"
    assert entry.sha256 == "a" * 64


# ---------------------------------------------------------------------------
# Проверка состояния карантина
# ---------------------------------------------------------------------------


async def test_is_quarantined_for_pending_file():
    file = _file()
    assert await is_quarantined(file.id, _session_with(file)) is True


async def test_is_quarantined_for_infected_and_error():
    for status in (ScanStatus.INFECTED, ScanStatus.ERROR):
        file = _file(scan_status=status, storage_key=f"files/{status.value}/file.pdf")
        assert await is_quarantined(file.id, _session_with(file)) is True


async def test_is_not_quarantined_for_clean_released_file():
    file = _file(scan_status=ScanStatus.CLEAN, storage_key="files/x/file.pdf")
    assert await is_quarantined(file.id, _session_with(file)) is False


async def test_is_quarantined_false_for_missing_or_invalid_id():
    assert await is_quarantined(uuid4(), _FakeSession()) is False
    assert await is_quarantined("not-a-uuid", _FakeSession()) is False


# ---------------------------------------------------------------------------
# Выпуск из карантина
# ---------------------------------------------------------------------------


async def test_release_from_quarantine_moves_object_and_marks_clean(storage):
    file = _file()
    _put(storage, file)
    session = _session_with(file)

    released = await release_from_quarantine(file.id, session)

    assert released is file
    assert file.scan_status == ScanStatus.CLEAN
    assert file.storage_key.startswith(RELEASED_PREFIX)
    assert (file.bucket, file.storage_key) in storage.objects
    assert (file.bucket, "quarantine/x/file.pdf") not in storage.objects
    assert session.commits == 1


async def test_release_from_quarantine_is_idempotent(storage):
    file = _file(scan_status=ScanStatus.CLEAN, storage_key="files/x/file.pdf")
    _put(storage, file)
    session = _session_with(file)

    await release_from_quarantine(file.id, session)

    assert file.storage_key == "files/x/file.pdf"
    assert file.scan_status == ScanStatus.CLEAN


async def test_release_from_quarantine_refuses_infected_file(storage):
    file = _file(scan_status=ScanStatus.INFECTED)
    _put(storage, file)
    session = _session_with(file)

    with pytest.raises(QuarantineError, match="Заражённый"):
        await release_from_quarantine(file.id, session)

    assert file.storage_key.startswith(QUARANTINE_PREFIX)
    assert session.commits == 0


async def test_release_from_quarantine_refuses_deleted_file(storage):
    file = _file(deleted_at="2026-11-01")
    _put(storage, file)
    session = _session_with(file)

    with pytest.raises(QuarantineError, match="удалён"):
        await release_from_quarantine(file.id, session)


async def test_release_from_quarantine_returns_none_for_missing_file(storage):
    assert await release_from_quarantine(uuid4(), _FakeSession()) is None
    assert await release_from_quarantine("bad-id", _FakeSession()) is None


# ---------------------------------------------------------------------------
# scan_file: ClamAV и FileScanResult
# ---------------------------------------------------------------------------


async def test_scan_file_without_clamav_is_skipped(monkeypatch):
    file = _file()
    session = _session_with(file)
    monkeypatch.setattr(service.settings, "clamav_host", None)
    monkeypatch.setattr(service, "read_object", lambda bucket, key: b"file-bytes")

    result = await scan_file(file.id, session)

    assert result.status == ScanStatus.SKIPPED
    assert result.scanner == "local"
    assert "не настроен" in result.details
    assert file.scan_status == ScanStatus.SKIPPED
    assert session.added[0] is result
    assert session.commits == 1


async def test_scan_file_records_infected_result(monkeypatch):
    file = _file()
    session = _session_with(file)
    monkeypatch.setattr(service.settings, "clamav_host", "clamav")
    monkeypatch.setattr(service, "read_object", lambda bucket, key: b"virus")

    async def infected(data):
        return service.ScanOutcome(ScanStatus.INFECTED, "clamav", "Eicar-Test-Signature")

    monkeypatch.setattr(service, "scan_content", infected)

    result = await scan_file(file.id, session)

    assert result.status == ScanStatus.INFECTED
    assert result.scanner == "clamav"
    assert "Eicar-Test-Signature" in result.details
    assert file.scan_status == ScanStatus.INFECTED


async def test_scan_file_records_storage_error(monkeypatch):
    file = _file()
    session = _session_with(file)

    def broken(bucket, key):
        raise ConnectionError("minio down")

    monkeypatch.setattr(service, "read_object", broken)

    result = await scan_file(file.id, session)

    assert result.status == ScanStatus.ERROR
    assert "minio down" in result.details
    assert file.scan_status == ScanStatus.ERROR


async def test_scan_file_rejects_unknown_file():
    with pytest.raises(FileRecordNotFound):
        await scan_file(uuid4(), _FakeSession())


# ---------------------------------------------------------------------------
# release_after_scan
# ---------------------------------------------------------------------------


async def test_release_after_scan_requires_clean_status():
    for status in (
        ScanStatus.PENDING,
        ScanStatus.INFECTED,
        ScanStatus.ERROR,
        ScanStatus.SKIPPED,
    ):
        file = _file(scan_status=status)
        with pytest.raises(FileScanError):
            await release_after_scan(file.id, _session_with(file))


async def test_release_after_scan_releases_clean_file(storage):
    file = _file(scan_status=ScanStatus.CLEAN)
    _put(storage, file)
    session = _session_with(file)

    released = await release_after_scan(file.id, session)

    assert released is file
    assert file.storage_key.startswith(RELEASED_PREFIX)
    assert file.scan_status == ScanStatus.CLEAN


async def test_release_after_scan_rejects_unknown_file():
    with pytest.raises(FileRecordNotFound):
        await release_after_scan(uuid4(), _FakeSession())


# ---------------------------------------------------------------------------
# upload_file: полный цикл
# ---------------------------------------------------------------------------


def _active_user() -> User:
    return User(
        id=uuid4(),
        telegram_id=123456789,
        username="uploader",
        display_name="Загружающий",
        role_id=uuid4(),
        status=UserStatus.ACTIVE,
    )


async def test_upload_file_full_cycle(monkeypatch):
    user = _active_user()
    session = _FakeSession({(User, user.id): user})
    calls = []

    async def allowed(*_args, **_kwargs):
        calls.append("permission")

    async def fake_quarantine(file_bytes, filename, _session, **kwargs):
        calls.append(("quarantine", filename, kwargs))
        file = _file(storage_key=build_storage_key(QUARANTINE_PREFIX, filename))
        file.uploaded_by = kwargs["uploaded_by"]
        file.mime_type = kwargs["mime_type"]
        file.sha256 = kwargs["sha256"]
        _session.add(file)
        await _session.flush()
        return file

    async def fake_scan_file(file_id, _session):
        calls.append("scan")
        file = _session.objects[(File, file_id)]
        file.scan_status = ScanStatus.CLEAN
        return SimpleNamespace(status=ScanStatus.CLEAN)

    async def fake_scan_content(data):
        calls.append("scan")
        return service.ScanOutcome(ScanStatus.CLEAN, "clamav", "Угроз не обнаружено")

    async def fake_release(file_id, _session):
        calls.append(("release", file_id))
        file = _session.objects[(File, file_id)]
        file.storage_key = file.storage_key.replace(QUARANTINE_PREFIX, RELEASED_PREFIX, 1)
        file.scan_status = ScanStatus.CLEAN
        return file

    async def fake_create_record(_session, **kwargs):
        calls.append(("record", kwargs))
        return SimpleNamespace(id=uuid4(), title=kwargs["title"], fields=[])

    async def fake_add_attachment(_session, record_id, file_id):
        calls.append(("attach", record_id, file_id))

    async def fake_log_access(file_id, user_id, action, result, _session):
        calls.append(("access", action, result))

    async def fake_log_audit(_session, *args, **kwargs):
        calls.append(("audit", args, kwargs))

    monkeypatch.setattr(service.PermissionService, "require", staticmethod(allowed))
    monkeypatch.setattr(service, "quarantine_file", fake_quarantine)
    monkeypatch.setattr(service, "scan_file", fake_scan_file)
    monkeypatch.setattr(service, "scan_content", fake_scan_content)
    monkeypatch.setattr(service, "release_after_scan", fake_release)
    monkeypatch.setattr(service, "create_record", fake_create_record)
    monkeypatch.setattr(service, "add_attachment", fake_add_attachment)
    monkeypatch.setattr(service, "log_file_access", fake_log_access)
    monkeypatch.setattr(service, "log_audit", fake_log_audit)

    record = await service.upload_file(
        session,
        category_code="general",
        title="",
        description=None,
        file_bytes=b"%PDF-1.7\ncontent",
        filename="../../договор.pdf",
        content_type="application/pdf",
        created_by_id=user.id,
    )

    assert record.title == "договор.pdf"
    names = [call[0] if isinstance(call, tuple) else call for call in calls]
    assert names == [
        "permission",
        "quarantine",
        "scan",
        "release",
        "record",
        "attach",
        "access",
        "audit",
    ]
    record_fields = next(call[1] for call in calls if call[0] == "record")["fields"]
    assert record_fields["filename"] == "договор.pdf"
    assert record_fields["mime_type"] == "application/pdf"
    assert len(record_fields["sha256"]) == 64
    assert record_fields["scan_status"] == "CLEAN"


async def test_upload_file_rejects_missing_user(monkeypatch):
    monkeypatch.setattr(
        service,
        "quarantine_file",
        lambda *a, **k: pytest.fail("quarantine must not be called"),
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


async def test_upload_file_rejects_bad_extension_before_quarantine(monkeypatch):
    user = _active_user()
    session = _FakeSession({(User, user.id): user})

    async def allowed(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service.PermissionService, "require", staticmethod(allowed))
    monkeypatch.setattr(
        service,
        "quarantine_file",
        lambda *a, **k: pytest.fail("quarantine must not be called"),
    )

    with pytest.raises(service.FileExtensionMismatch):
        await service.upload_file(
            session,
            category_code="general",
            title="",
            description=None,
            file_bytes=b"%PDF-1.7\ncontent",
            filename="document.txt",
            content_type="application/pdf",
            created_by_id=user.id,
        )


async def test_upload_file_keeps_non_clean_file_in_quarantine(monkeypatch):
    user = _active_user()
    session = _FakeSession({(User, user.id): user})
    released = []

    async def allowed(*_args, **_kwargs):
        return None

    async def fake_quarantine(file_bytes, filename, _session, **kwargs):
        file = _file(storage_key=build_storage_key(QUARANTINE_PREFIX, filename))
        _session.add(file)
        await _session.flush()
        return file

    async def fake_scan_file(file_id, _session):
        file = _session.objects[(File, file_id)]
        file.scan_status = ScanStatus.SKIPPED
        return SimpleNamespace(status=ScanStatus.SKIPPED)

    async def fake_scan_content(file_bytes):
        return service.ScanOutcome(ScanStatus.SKIPPED, "local", "ClamAV не настроен")

    async def fake_release(*_args, **_kwargs):
        released.append(True)
        pytest.fail("release must not be called for a non-clean file")

    async def fake_create_record(_session, **kwargs):
        return SimpleNamespace(id=uuid4(), title=kwargs["title"], fields=[])

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service.PermissionService, "require", staticmethod(allowed))
    monkeypatch.setattr(service, "quarantine_file", fake_quarantine)
    monkeypatch.setattr(service, "scan_file", fake_scan_file)
    monkeypatch.setattr(service, "scan_content", fake_scan_content)
    monkeypatch.setattr(service, "release_after_scan", fake_release)
    monkeypatch.setattr(service, "create_record", fake_create_record)
    monkeypatch.setattr(service, "add_attachment", noop)
    monkeypatch.setattr(service, "log_file_access", noop)
    monkeypatch.setattr(service, "log_audit", noop)

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

    assert released == []
