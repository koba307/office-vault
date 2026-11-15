import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from app.core.config import settings
from app.database.session import engine

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_ADVISORY_LOCK_ID = 0x4F564D4947524154

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

LEGACY_BASELINE_ENUMS = {
    "rolecode": {"SUPERADMIN", "BOSS", "EMPLOYEE"},
    "userstatus": {"ACTIVE", "BLOCKED", "PENDING"},
    "recordtype": {"CREDENTIALS", "SIP", "LINKS", "SETTINGS", "INSTRUCTIONS", "FILES", "OTHER"},
    "accessmode": {"ALL_EMPLOYEES", "BOSSES_ONLY", "GROUP", "SELECTED_USERS", "PRIVATE"},
}

MIGRATION_TABLES = {
    "0001": set(LEGACY_BASELINE_COLUMNS),
    "0002": set(LEGACY_BASELINE_COLUMNS)
    | {"roles", "permissions", "role_permissions", "user_permissions", "user_group_permissions"},
    "0003": set(LEGACY_BASELINE_COLUMNS)
    | {
        "roles",
        "permissions",
        "role_permissions",
        "user_permissions",
        "user_group_permissions",
        "category_permissions",
        "category_group_permissions",
        "category_user_permissions",
    },
    "0004": set(LEGACY_BASELINE_COLUMNS)
    | {
        "roles",
        "permissions",
        "role_permissions",
        "user_permissions",
        "user_group_permissions",
        "category_permissions",
        "category_group_permissions",
        "category_user_permissions",
        "tags",
        "record_tags",
        "record_versions",
        "record_permissions",
        "record_group_permissions",
        "record_user_permissions",
    },
    "0005": set(LEGACY_BASELINE_COLUMNS)
    | {
        "roles",
        "permissions",
        "role_permissions",
        "user_permissions",
        "user_group_permissions",
        "category_permissions",
        "category_group_permissions",
        "category_user_permissions",
        "tags",
        "record_tags",
        "record_versions",
        "record_permissions",
        "record_group_permissions",
        "record_user_permissions",
        "files",
        "file_versions",
        "record_files",
        "file_access_logs",
        "file_scan_results",
    },
    "0006": set(LEGACY_BASELINE_COLUMNS)
    | {
        "roles",
        "permissions",
        "role_permissions",
        "user_permissions",
        "user_group_permissions",
        "category_permissions",
        "category_group_permissions",
        "category_user_permissions",
        "tags",
        "record_tags",
        "record_versions",
        "record_permissions",
        "record_group_permissions",
        "record_user_permissions",
        "files",
        "file_versions",
        "record_files",
        "file_access_logs",
        "file_scan_results",
        "recent_records",
        "user_settings",
        "import_jobs",
        "import_errors",
        "export_jobs",
    },
    "0007": set(LEGACY_BASELINE_COLUMNS)
    | {
        "roles",
        "permissions",
        "role_permissions",
        "user_permissions",
        "user_group_permissions",
        "category_permissions",
        "category_group_permissions",
        "category_user_permissions",
        "tags",
        "record_tags",
        "record_versions",
        "record_permissions",
        "record_group_permissions",
        "record_user_permissions",
        "files",
        "file_versions",
        "record_files",
        "file_access_logs",
        "file_scan_results",
        "recent_records",
        "user_settings",
        "import_jobs",
        "import_errors",
        "export_jobs",
        "security_events",
        "backups",
        "system_settings",
        "encryption_key_versions",
        "background_jobs",
    },
    "0008": set(LEGACY_BASELINE_COLUMNS)
    | {
        "roles",
        "permissions",
        "role_permissions",
        "user_permissions",
        "user_group_permissions",
        "category_permissions",
        "category_group_permissions",
        "category_user_permissions",
        "tags",
        "record_tags",
        "record_versions",
        "record_permissions",
        "record_group_permissions",
        "record_user_permissions",
        "files",
        "file_versions",
        "record_files",
        "file_access_logs",
        "file_scan_results",
        "recent_records",
        "user_settings",
        "import_jobs",
        "import_errors",
        "export_jobs",
        "security_events",
        "backups",
        "system_settings",
        "encryption_key_versions",
        "background_jobs",
        "invites",
    },
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
        revisions = set(conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalars())
        if not revisions and tables == {"alembic_version"}:
            return "empty"
        if len(revisions) != 1:
            raise RuntimeError(
                f"Expected one Alembic revision, found {sorted(revisions)}; "
                "reconcile the migration state before startup."
            )
        revision = next(iter(revisions))
        required_tables = MIGRATION_TABLES.get(revision)
        if required_tables is None:
            raise RuntimeError(
                f"Database revision {revision!r} is not supported by this application version."
            )
        actual_tables = tables - {"alembic_version"}
        if actual_tables != required_tables:
            missing = sorted(required_tables - actual_tables)
            extra = sorted(actual_tables - required_tables)
            raise RuntimeError(
                f"Database revision {revision} has an inconsistent table set. "
                f"Missing tables: {missing or 'none'}; unexpected tables: {extra or 'none'}."
            )
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

    actual_enums = {
        enum["name"]: set(enum["labels"]) for enum in inspector.get_enums()
    }
    for enum_name, required_labels in LEGACY_BASELINE_ENUMS.items():
        if actual_enums.get(enum_name) != required_labels:
            raise RuntimeError(
                f"Legacy enum {enum_name!r} does not match the supported baseline. "
                "Back up the database and reconcile its enum values before enabling automatic migrations."
            )

    return "legacy"


async def run_migrations() -> None:
    cfg = _alembic_config()

    async with engine.connect() as lock_connection:
        lock_acquired = False
        try:
            await lock_connection.execute(
                text("SELECT pg_advisory_lock(:lock_id)"),
                {"lock_id": MIGRATION_ADVISORY_LOCK_ID},
            )
            lock_acquired = True
            await lock_connection.commit()

            state = await lock_connection.run_sync(_database_state)
            await lock_connection.commit()

            if state == "legacy":
                # Stamp only the exact pre-Alembic schema, never infer from a users table alone.
                await asyncio.to_thread(command.stamp, cfg, "0001")
                logger.info("Stamped validated legacy database at revision 0001")

            await asyncio.to_thread(command.upgrade, cfg, "head")
            logger.info("Database migrations applied to head")
        finally:
            if lock_acquired:
                try:
                    await lock_connection.rollback()
                    await lock_connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": MIGRATION_ADVISORY_LOCK_ID},
                    )
                    await lock_connection.commit()
                except Exception:
                    await lock_connection.invalidate()
                    raise


async def check_migrations() -> None:
    cfg = _alembic_config()
    await asyncio.to_thread(command.check, cfg)
