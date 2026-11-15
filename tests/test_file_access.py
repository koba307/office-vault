from uuid import uuid4

import pytest

from app.database.models import Category, File, FileAccessLog, PermissionCode, ScanStatus, User
from app.services.files import access, service
from app.services.files.access import (
    ACCESS_DENIED,
    ACCESS_ERROR,
    ACCESS_SUCCESS,
    check_file_access,
    check_file_download,
    check_file_upload,
    log_file_access,
)
from app.services.files.service import download_file_bytes
from app.services.permissions import PermissionDenied, PermissionService
from app.services.permissions.errors import DenyReason

# ---------------------------------------------------------------------------
# Заглушки
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, objects=None):
        self.objects = objects or {}
        self.added = []
        self.commits = 0

    async def get(self, model, key):
        return self.objects.get((model, key))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _user():
    return User(id=uuid4(), telegram_id=1, display_name="Тест")


def _file(**overrides):
    values = {
        "id": uuid4(),
        "original_name": "договор.docx",
        "storage_key": "attachments/x/договор.docx",
        "bucket": "office-vault",
        "mime_type": "application/vnd.openxmlformats",
        "size": 10,
        "sha256": "f" * 64,
        "scan_status": ScanStatus.CLEAN,
        "uploaded_by": uuid4(),
        "deleted_at": None,
    }
    values.update(overrides)
    return File(**values)


class _Recorder:
    """Подменяет PermissionService.can и запоминает аргументы вызова."""

    def __init__(self, result=True):
        self.result = result
        self.calls = []

    async def __call__(self, user, action, target_type, target_id, session):
        self.calls.append((action, target_type, target_id))
        return self.result


# ---------------------------------------------------------------------------
# Проверки прав: какое действие и какой объект передаются в PermissionService
# ---------------------------------------------------------------------------


async def test_check_file_access_uses_view_on_file(monkeypatch):
    recorder = _Recorder(result=True)
    monkeypatch.setattr(PermissionService, "can", staticmethod(recorder))
    file_id = uuid4()

    assert await check_file_access(_user(), file_id, _FakeSession()) is True
    assert recorder.calls == [(PermissionCode.VIEW, "file", file_id)]


async def test_check_file_download_uses_download_on_file(monkeypatch):
    recorder = _Recorder(result=False)
    monkeypatch.setattr(PermissionService, "can", staticmethod(recorder))
    file_id = uuid4()

    assert await check_file_download(_user(), file_id, _FakeSession()) is False
    assert recorder.calls == [(PermissionCode.DOWNLOAD, "file", file_id)]


async def test_check_file_upload_denies_unknown_category(monkeypatch):
    monkeypatch.setattr(access, "resolve_category_id", _none_async)
    monkeypatch.setattr(PermissionService, "can", staticmethod(_must_not_be_called))

    assert await check_file_upload(_user(), "missing", _FakeSession()) is False


async def test_check_file_upload_denies_inactive_category(monkeypatch):
    category_id = uuid4()
    category = Category(id=category_id, code="holodka", name="Холодка", status="ARCHIVED")
    session = _FakeSession({(Category, category_id): category})
    monkeypatch.setattr(access, "resolve_category_id", _const_async(category_id))
    monkeypatch.setattr(PermissionService, "can", staticmethod(_must_not_be_called))

    assert await check_file_upload(_user(), "holodka", session) is False


async def test_check_file_upload_checks_upload_permission_on_active_category(monkeypatch):
    category_id = uuid4()
    category = Category(id=category_id, code="holodka", name="Холодка", status="ACTIVE")
    session = _FakeSession({(Category, category_id): category})
    recorder = _Recorder(result=True)
    monkeypatch.setattr(access, "resolve_category_id", _const_async(category_id))
    monkeypatch.setattr(PermissionService, "can", staticmethod(recorder))

    assert await check_file_upload(_user(), "holodka", session) is True
    assert recorder.calls == [(PermissionCode.UPLOAD, "category", category_id)]


async def _none_async(*_args):
    return None


async def _must_not_be_called(*_args, **_kwargs):
    raise AssertionError("PermissionService.can вызван для недоступной категории")


def _const_async(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


# ---------------------------------------------------------------------------
# Журнал обращений
# ---------------------------------------------------------------------------


async def test_log_file_access_writes_entry_and_commits():
    file_id, user_id = uuid4(), uuid4()
    session = _FakeSession()

    await log_file_access(file_id, user_id, "DOWNLOAD", ACCESS_DENIED, session)

    entry = session.added[0]
    assert isinstance(entry, FileAccessLog)
    assert (entry.file_id, entry.user_id, entry.action, entry.result) == (
        file_id,
        user_id,
        "DOWNLOAD",
        ACCESS_DENIED,
    )
    assert session.commits == 1


async def test_log_file_access_accepts_missing_user():
    session = _FakeSession()
    await log_file_access(uuid4(), None, "DOWNLOAD", ACCESS_SUCCESS, session)
    assert session.added[0].user_id is None


# ---------------------------------------------------------------------------
# Скачивание: проверка прав перед чтением и запись в журнал
# ---------------------------------------------------------------------------


def _deny(reason):
    async def _inner(*_args, **_kwargs):
        raise PermissionDenied(reason)

    return _inner


async def test_download_is_denied_when_file_not_clean(monkeypatch):
    file = _file(scan_status=ScanStatus.PENDING)
    session = _FakeSession({(File, file.id): file})
    user = _user()
    monkeypatch.setattr(
        PermissionService, "require", staticmethod(_deny(DenyReason.FILE_NOT_CLEAN))
    )
    monkeypatch.setattr(service, "read_object", lambda *a: pytest.fail("read before check"))

    with pytest.raises(PermissionDenied) as exc_info:
        await download_file_bytes(session, user=user, file_id=file.id)

    assert exc_info.value.reason == DenyReason.FILE_NOT_CLEAN
    entry = session.added[0]
    assert (entry.action, entry.result, entry.user_id) == ("DOWNLOAD", ACCESS_DENIED, user.id)


async def test_download_of_missing_file_is_not_logged(monkeypatch):
    # Журнал ссылается на files.id по внешнему ключу: для несуществующего файла строку не пишем
    session = _FakeSession()
    monkeypatch.setattr(
        PermissionService, "require", staticmethod(_deny(DenyReason.FILE_NOT_FOUND))
    )

    with pytest.raises(PermissionDenied):
        await download_file_bytes(session, user=_user(), file_id=uuid4())

    assert session.added == []


async def test_download_returns_bytes_and_logs_success(monkeypatch):
    file = _file()
    session = _FakeSession({(File, file.id): file})
    user = _user()

    async def allowed(*_args, **_kwargs):
        return None

    monkeypatch.setattr(PermissionService, "require", staticmethod(allowed))
    monkeypatch.setattr(service, "read_object", lambda bucket, key: b"contract-bytes")

    result_file, data = await download_file_bytes(session, user=user, file_id=file.id)

    assert result_file is file
    assert data == b"contract-bytes"
    entry = session.added[0]
    assert (entry.action, entry.result) == ("DOWNLOAD", ACCESS_SUCCESS)


async def test_download_logs_error_when_storage_fails(monkeypatch):
    file = _file()
    session = _FakeSession({(File, file.id): file})

    async def allowed(*_args, **_kwargs):
        return None

    def broken(*_args):
        raise ConnectionError("minio down")

    monkeypatch.setattr(PermissionService, "require", staticmethod(allowed))
    monkeypatch.setattr(service, "read_object", broken)

    with pytest.raises(ConnectionError):
        await download_file_bytes(session, user=_user(), file_id=file.id)

    assert session.added[0].result == ACCESS_ERROR
