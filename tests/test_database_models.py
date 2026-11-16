from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.database.models import AccessMode, Base, FieldType, PermissionCode, RecordType, RoleCode


def test_tz_v2_tables_are_registered():
    expected = {
        "users",
        "roles",
        "permissions",
        "role_permissions",
        "user_permissions",
        "groups",
        "group_members",
        "user_group_permissions",
        "categories",
        "category_permissions",
        "category_group_permissions",
        "category_user_permissions",
        "records",
        "record_fields",
        "record_permissions",
        "record_group_permissions",
        "record_user_permissions",
        "tags",
        "record_tags",
        "record_versions",
        "files",
        "file_versions",
        "record_files",
        "file_access_logs",
        "file_scan_results",
        "favorites",
        "recent_records",
        "user_settings",
        "import_jobs",
        "import_errors",
        "export_jobs",
        "audit_logs",
        "security_events",
        "backups",
        "system_settings",
        "encryption_key_versions",
        "background_jobs",
    }
    assert expected <= set(Base.metadata.tables)


def test_required_enum_values_exist():
    assert {role.value for role in RoleCode} == {"SUPERADMIN", "BOSS", "EMPLOYEE"}
    assert {record_type.value for record_type in RecordType} == {
        "CREDENTIALS",
        "SIP",
        "LINKS",
        "SETTINGS",
        "INSTRUCTIONS",
        "FILES",
        "OTHER",
    }
    assert {access_mode.value for access_mode in AccessMode} == {
        "ALL_EMPLOYEES",
        "BOSSES_ONLY",
        "GROUP",
        "SELECTED_USERS",
        "PRIVATE",
    }
    assert {field_type.value for field_type in FieldType} == {
        "TEXT",
        "LONG_TEXT",
        "PASSWORD",
        "SECRET",
        "URL",
        "EMAIL",
        "PHONE",
        "NUMBER",
        "BOOLEAN",
        "DATE",
        "DATETIME",
        "JSON",
        "FILE_REFERENCE",
        "MULTISELECT",
    }
    assert len(PermissionCode) >= 10


def test_alembic_has_one_expected_head():
    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "app/database/migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == ["0009"]
