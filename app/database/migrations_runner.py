import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.core.config import settings
from app.database.session import engine

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

LEGACY_BASELINE_COLUMNS = {
    "users": {
        "id",
        "telegram_id",
        "username",
        "display_name",
        "role",
        "status",
        "created_at",
        "updated_at",
        "last_seen_at",
    },
    "groups": {"id", "code", "name", "description", "is_system", "created_at"},
    "group_members": {"group_id", "user_id", "assigned_by", "assigned_at"},
    "categories": {
        "id",
        "parent_id",
        "code",
        "name",
        "category_type",
        "sort_order",
        "status",
        "created_at",
        "updated_at",
    },
    "records": {
        "id",
        "category_id",
        "record_type",
        "title",
        "description",
        "status",
        "access_mode",
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
        "version",
    },
    "record_fields": {
        "id",
        "record_id",
        "field_key",
        "field_label",
        "field_type",
        "value_text",
        "value_json",
        "encrypted_value",
        "encryption_key_version",
        "sort_order",
    },
    "audit_logs": {
        "id",
        "actor_user_id",
        "action",
        "object_type",
        "object_id",
        "result",
        "metadata",
        "created_at",
    },
    "favorites": {"user_id", "record_id", "created_at"},
}


def _alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "app/database/migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    cfg.set_main_option("prepend_sys_path", str(PROJECT_ROOT))
    return cfg


def _database_state(conn) -> str:
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    if "alembic_version" in tables:
        return "versioned"

    baseline_tables = set(LEGACY_BASELINE_COLUMNS)
    baseline_present = tables & baseline_tables
    if not baseline_present and not tables:
        return "empty"
    if baseline_present != baseline_tables or tables != baseline_tables:
        missing = sorted(baseline_tables - tables)
        extra = sorted(tables - baseline_tables)
        raise RuntimeError(
            "Database has no Alembic revision and does not match the supported legacy schema. "
            f"Missing tables: {missing or 'none'}; unexpected tables: {extra or 'none'}. "
            "Back up the database and reconcile its schema before enabling automatic migrations."
        )

    for table_name, required_columns in LEGACY_BASELINE_COLUMNS.items():
        actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
        if actual_columns != required_columns:
            missing = sorted(required_columns - actual_columns)
            extra = sorted(actual_columns - required_columns)
            raise RuntimeError(
                f"Legacy table {table_name!r} has an unsupported shape. "
                f"Missing columns: {missing or 'none'}; unexpected columns: {extra or 'none'}. "
                "Back up the database and reconcile its schema before enabling automatic migrations."
            )

    return "legacy"


async def run_migrations() -> None:
    cfg = _alembic_config()

    async with engine.connect() as conn:
        state = await conn.run_sync(_database_state)

    if state == "legacy":
        # Stamp only the exact pre-Alembic schema; never infer from a users table alone.
        await asyncio.to_thread(command.stamp, cfg, "0001")
        logger.info("Stamped validated legacy database at revision 0001")

    await asyncio.to_thread(command.upgrade, cfg, "head")
    logger.info("Database migrations applied to head")


async def check_migrations() -> None:
    cfg = _alembic_config()
    await asyncio.to_thread(command.check, cfg)
