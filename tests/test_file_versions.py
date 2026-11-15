import sys
import types
from hashlib import sha256
from uuid import uuid4

import pytest

from app.database.models import File, FileVersion, ScanStatus
from app.services.files import service, versions
from app.services.files.versions import (
    FileNotFound,
    FileVersionNotFound,
    current_version_number,
    replace_file,
    restore_file_version,
)

# ---------------------------------------------------------------------------
# Заглушки: сессия без БД и файл без ORM-сессии
# ---------------------------------------------------------------------------


class _FakeSession:
    """Минимальная имитация AsyncSession: get по ключу, scalar из очереди ответов."""

    def __init__(self, objects=None, scalars=None):
        self.objects = objects or {}
        self.scalar_values = list(scalars or [])
        self.added = []
        self.commits = 0
        self.refreshed = []

    async def get(self, model, key):
        return self.objects.get((model, key))

    async def scalar(self, _stmt):
        return self.scalar_values.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        self.refreshed.append(obj)


def _file(**overrides):
    values = {
        "id": uuid4(),
        "original_name": "акт.pdf",
        "storage_key": "attachments/old/акт.pdf",
        "bucket": "office-vault",
        "mime_type": "application/pdf",
        "extension": "pdf",
        "size": 5,
        "sha256": sha256(b"old!!").hexdigest(),
        "scan_status": ScanStatus.CLEAN,
        "uploaded_by": uuid4(),
        "deleted_at": None,
    }
    values.update(overrides)
    return File(**values)


def _async_value(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


# ---------------------------------------------------------------------------
# Номера версий
# ---------------------------------------------------------------------------


async def test_current_version_number_starts_at_one_without_snapshots():
    session = _FakeSession(scalars=[None])
    assert await current_version_number(uuid4(), session) == 1


async def test_current_version_number_is_next_after_last_snapshot():
    session = _FakeSession(scalars=[3])
    assert await current_version_number(uuid4(), session) == 4


# ---------------------------------------------------------------------------
# Замена файла
# ---------------------------------------------------------------------------


async def test_replace_file_keeps_old_content_as_version_and_stores_new(monkeypatch):
    file = _file()
    old_key, old_sha = file.storage_key, file.sha256
    session = _FakeSession({(File, file.id): file}, scalars=[3])
    stored = []

    def fake_store(data, name, content_type):
        stored.append((name, content_type))
        return "office-vault-test"

    monkeypatch.setattr(versions, "scan_file_bytes", _async_value(ScanStatus.CLEAN))
    monkeypatch.setattr(versions, "store_object", fake_store)

    new_bytes = b"new content"
    result = await replace_file(file.id, new_bytes, session)

    snapshot = session.added[0]
    assert isinstance(snapshot, FileVersion)
    assert snapshot.version == 4
    assert snapshot.storage_key == old_key
    assert snapshot.sha256 == old_sha

    assert result is file
    assert file.storage_key.startswith("attachments/")
    assert file.storage_key != old_key
    assert file.bucket == "office-vault-test"
    assert file.size == len(new_bytes)
    assert file.sha256 == sha256(new_bytes).hexdigest()
    assert file.scan_status == ScanStatus.CLEAN
    assert stored == [(file.storage_key, "application/pdf")]
    assert session.commits == 1


async def test_replace_file_does_not_store_infected_bytes(monkeypatch):
    file = _file()
    session = _FakeSession({(File, file.id): file}, scalars=[None])
    stored = []

    async def infected(_data):
        raise ValueError("Файл заражён: Eicar")

    monkeypatch.setattr(versions, "scan_file_bytes", infected)
    monkeypatch.setattr(versions, "store_object", lambda *a: stored.append(a))

    with pytest.raises(ValueError, match="заражён"):
        await replace_file(file.id, b"virus", session)

    assert stored == []
    assert session.added == []
    assert session.commits == 0


async def test_replace_file_rejects_deleted_file(monkeypatch):
    file = _file(deleted_at="2026-11-01")
    session = _FakeSession({(File, file.id): file})
    monkeypatch.setattr(versions, "store_object", lambda *a: pytest.fail("store called"))

    with pytest.raises(FileNotFound):
        await replace_file(file.id, b"data", session)


async def test_replace_file_rejects_unknown_file(monkeypatch):
    session = _FakeSession()
    with pytest.raises(FileNotFound):
        await replace_file(uuid4(), b"data", session)


# ---------------------------------------------------------------------------
# Восстановление версии
# ---------------------------------------------------------------------------


async def test_restore_file_version_makes_old_content_current_and_keeps_current_as_version():
    file = _file(storage_key="attachments/current", size=7, sha256="c" * 64)
    target = FileVersion(
        file_id=file.id,
        version=1,
        storage_key="attachments/v1",
        size=5,
        sha256="a" * 64,
        scan_status=ScanStatus.CLEAN,
    )
    # Первый scalar — найденная версия, второй — номер для снимка текущего содержимого
    session = _FakeSession({(File, file.id): file}, scalars=[target, 2])

    result = await restore_file_version(file.id, 1, session)

    assert result is file
    assert file.storage_key == "attachments/v1"
    assert file.size == 5
    assert file.sha256 == "a" * 64
    snapshot = session.added[0]
    assert snapshot.version == 3
    assert snapshot.storage_key == "attachments/current"
    assert session.commits == 1


async def test_restore_file_version_raises_when_version_missing():
    file = _file()
    session = _FakeSession({(File, file.id): file}, scalars=[None])

    with pytest.raises(FileVersionNotFound):
        await restore_file_version(file.id, 9, session)

    assert session.added == []
    assert session.commits == 0


# ---------------------------------------------------------------------------
# Сканирование (ClamAV): поведение без сканера, чистый, заражённый и недоступный
# ---------------------------------------------------------------------------


def _fake_pyclamd(monkeypatch, scan_result=None, error=None):
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


async def test_scan_is_skipped_without_clamav_host(monkeypatch):
    monkeypatch.setattr(service.settings, "clamav_host", None)
    assert await service.scan_file_bytes(b"data") == ScanStatus.SKIPPED


async def test_scan_returns_clean_for_clean_content(monkeypatch):
    monkeypatch.setattr(service.settings, "clamav_host", "clamav")
    _fake_pyclamd(monkeypatch, scan_result=None)
    assert await service.scan_file_bytes(b"data") == ScanStatus.CLEAN


async def test_scan_raises_for_infected_content(monkeypatch):
    monkeypatch.setattr(service.settings, "clamav_host", "clamav")
    _fake_pyclamd(monkeypatch, scan_result={"stream": ("FOUND", "Eicar")})
    with pytest.raises(ValueError, match="заражён"):
        await service.scan_file_bytes(b"data")


async def test_scan_returns_error_when_scanner_unavailable(monkeypatch):
    monkeypatch.setattr(service.settings, "clamav_host", "clamav")
    _fake_pyclamd(monkeypatch, error=ConnectionError("no clamd"))
    assert await service.scan_file_bytes(b"data") == ScanStatus.ERROR
