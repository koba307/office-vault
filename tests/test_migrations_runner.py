import pytest

from app.core.config import settings
from app.database.migrations_runner import (
    LEGACY_BASELINE_COLUMNS,
    _alembic_config,
    _database_state,
)


class FakeInspector:
    def __init__(self, tables: set[str], columns: dict[str, set[str]] | None = None):
        self.tables = tables
        self.columns = columns or {}

    def get_table_names(self) -> list[str]:
        return sorted(self.tables)

    def get_columns(self, table_name: str) -> list[dict[str, str]]:
        return [{"name": name} for name in sorted(self.columns[table_name])]


def test_empty_database_is_detected(monkeypatch):
    monkeypatch.setattr("app.database.migrations_runner.inspect", lambda _: FakeInspector(set()))
    assert _database_state(object()) == "empty"


def test_exact_legacy_schema_is_detected(monkeypatch):
    tables = set(LEGACY_BASELINE_COLUMNS)
    inspector = FakeInspector(tables, LEGACY_BASELINE_COLUMNS)
    monkeypatch.setattr("app.database.migrations_runner.inspect", lambda _: inspector)
    assert _database_state(object()) == "legacy"


def test_partial_legacy_schema_is_rejected(monkeypatch):
    inspector = FakeInspector({"users"}, {"users": {"id", "telegram_id"}})
    monkeypatch.setattr("app.database.migrations_runner.inspect", lambda _: inspector)
    with pytest.raises(RuntimeError, match="does not match the supported legacy schema"):
        _database_state(object())


def test_versioned_database_is_not_stamped_again(monkeypatch):
    inspector = FakeInspector({"alembic_version"})
    monkeypatch.setattr("app.database.migrations_runner.inspect", lambda _: inspector)
    assert _database_state(object()) == "versioned"


def test_alembic_config_preserves_percent_in_database_url(monkeypatch):
    url = "postgresql+asyncpg://officevault:encoded%25password@localhost:5432/officevault"
    monkeypatch.setattr(settings, "database_url", url)
    assert _alembic_config().get_main_option("sqlalchemy.url") == url
