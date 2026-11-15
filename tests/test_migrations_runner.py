import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.database import migrations_runner
from app.database.migrations_runner import (
    LEGACY_BASELINE_COLUMNS,
    LEGACY_BASELINE_ENUMS,
    MIGRATION_TABLES,
    _alembic_config,
    _database_state,
)


class FakeInspector:
    def __init__(
        self,
        tables: set[str],
        columns: dict[str, set[str]] | None = None,
        enums: dict[str, set[str]] | None = None,
    ):
        self.tables = tables
        self.columns = columns or {}
        self.enums = enums or LEGACY_BASELINE_ENUMS

    def get_table_names(self) -> list[str]:
        return sorted(self.tables)

    def get_columns(self, table_name: str) -> list[dict[str, str]]:
        return [{"name": name} for name in sorted(self.columns[table_name])]

    def get_enums(self) -> list[dict[str, object]]:
        return [
            {"name": name, "labels": sorted(labels)}
            for name, labels in self.enums.items()
        ]


class FakeScalarResult:
    def __init__(self, values: list[str]):
        self.values = values

    def scalars(self):
        return self

    def all(self) -> list[str]:
        return self.values

    def __iter__(self):
        return iter(self.values)


class FakeConnection:
    def __init__(self, revisions: list[str] | None = None):
        self.revisions = revisions or []

    def exec_driver_sql(self, _: str) -> FakeScalarResult:
        return FakeScalarResult(self.revisions)


def test_empty_database_is_detected(monkeypatch):
    monkeypatch.setattr("app.database.migrations_runner.inspect", lambda _: FakeInspector(set()))
    assert _database_state(object()) == "empty"


def test_exact_legacy_schema_is_detected(monkeypatch):
    tables = set(LEGACY_BASELINE_COLUMNS)
    inspector = FakeInspector(tables, LEGACY_BASELINE_COLUMNS)
    monkeypatch.setattr("app.database.migrations_runner.inspect", lambda _: inspector)
    assert _database_state(FakeConnection()) == "legacy"


def test_partial_legacy_schema_is_rejected(monkeypatch):
    inspector = FakeInspector({"users"}, {"users": {"id", "telegram_id"}})
    monkeypatch.setattr("app.database.migrations_runner.inspect", lambda _: inspector)
    with pytest.raises(RuntimeError, match="does not match the supported legacy schema"):
        _database_state(object())


def test_legacy_enum_extension_is_rejected_before_stamp(monkeypatch):
    enums = {name: set(labels) for name, labels in LEGACY_BASELINE_ENUMS.items()}
    enums["rolecode"].add("MANAGER")
    inspector = FakeInspector(set(LEGACY_BASELINE_COLUMNS), LEGACY_BASELINE_COLUMNS, enums)
    monkeypatch.setattr("app.database.migrations_runner.inspect", lambda _: inspector)
    with pytest.raises(RuntimeError, match="does not match the supported baseline"):
        _database_state(FakeConnection())


def test_empty_alembic_version_table_is_treated_as_empty(monkeypatch):
    inspector = FakeInspector({"alembic_version"})
    monkeypatch.setattr("app.database.migrations_runner.inspect", lambda _: inspector)
    assert _database_state(FakeConnection()) == "empty"


def test_versioned_database_requires_its_migration_tables(monkeypatch):
    inspector = FakeInspector({"alembic_version"})
    monkeypatch.setattr("app.database.migrations_runner.inspect", lambda _: inspector)
    with pytest.raises(RuntimeError, match="inconsistent table set"):
        _database_state(FakeConnection(["0007"]))


def test_complete_versioned_database_is_detected(monkeypatch):
    inspector = FakeInspector(MIGRATION_TABLES["0007"] | {"alembic_version"})
    monkeypatch.setattr("app.database.migrations_runner.inspect", lambda _: inspector)
    assert _database_state(FakeConnection(["0007"])) == "versioned"


def test_alembic_config_preserves_percent_in_database_url(monkeypatch):
    url = "postgresql+asyncpg://officevault:encoded%25password@localhost:5432/officevault"
    monkeypatch.setattr(settings, "database_url", url)
    assert _alembic_config().get_main_option("sqlalchemy.url") == url


@pytest.mark.asyncio
async def test_concurrent_migration_calls_are_serialized(monkeypatch):
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated PostgreSQL test database")

    engine = create_async_engine(database_url)
    monkeypatch.setattr(migrations_runner, "engine", engine)
    monkeypatch.setattr(settings, "database_url", database_url)
    try:
        await asyncio.gather(migrations_runner.run_migrations(), migrations_runner.run_migrations())
    finally:
        await engine.dispose()
