"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-15

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Enum types
    op.execute("CREATE TYPE rolecode AS ENUM ('SUPERADMIN', 'BOSS', 'EMPLOYEE')")
    op.execute("CREATE TYPE userstatus AS ENUM ('ACTIVE', 'BLOCKED', 'PENDING')")
    op.execute(
        "CREATE TYPE recordtype AS ENUM "
        "('CREDENTIALS', 'SIP', 'LINKS', 'SETTINGS', 'INSTRUCTIONS', 'FILES', 'OTHER')"
    )
    op.execute(
        "CREATE TYPE accessmode AS ENUM "
        "('ALL_EMPLOYEES', 'BOSSES_ONLY', 'GROUP', 'SELECTED_USERS', 'PRIVATE')"
    )

    rolecode = postgresql.ENUM(name="rolecode", create_type=False)
    userstatus = postgresql.ENUM(name="userstatus", create_type=False)
    recordtype = postgresql.ENUM(name="recordtype", create_type=False)
    accessmode = postgresql.ENUM(name="accessmode", create_type=False)

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("role", rolecode, nullable=False),
        sa.Column("status", userstatus, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)

    op.create_table(
        "groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("code", name="uq_groups_code"),
    )

    op.create_table(
        "group_members",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assigned_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], name="fk_group_members_group"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_group_members_user"),
        sa.ForeignKeyConstraint(["assigned_by"], ["users.id"], name="fk_group_members_assigned_by"),
        sa.PrimaryKeyConstraint("group_id", "user_id", name="pk_group_members"),
    )

    op.create_table(
        "categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("category_type", sa.String(32), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["parent_id"], ["categories.id"], name="fk_categories_parent"),
        sa.UniqueConstraint("code", name="uq_categories_code"),
    )

    op.create_table(
        "records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record_type", recordtype, nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("access_mode", accessmode, nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], name="fk_records_category"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_records_created_by"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name="fk_records_updated_by"),
    )

    op.create_table(
        "record_fields",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_key", sa.String(64), nullable=False),
        sa.Column("field_label", sa.String(128), nullable=False),
        sa.Column("field_type", sa.String(32), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_json", postgresql.JSONB(), nullable=True),
        sa.Column("encrypted_value", sa.Text(), nullable=True),
        sa.Column("encryption_key_version", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["record_id"], ["records.id"]
        ),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("object_type", sa.String(64), nullable=False),
        sa.Column("object_id", sa.String(64), nullable=True),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], name="fk_audit_actor"),
    )

    op.create_table(
        "favorites",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"]
        ),
        sa.ForeignKeyConstraint(
            ["record_id"], ["records.id"]
        ),
        sa.PrimaryKeyConstraint("user_id", "record_id", name="pk_favorites"),
    )


def downgrade() -> None:
    op.drop_table("favorites")
    op.drop_table("audit_logs")
    op.drop_table("record_fields")
    op.drop_table("records")
    op.drop_table("categories")
    op.drop_table("group_members")
    op.drop_table("groups")
    op.drop_table("users")

    op.execute("DROP TYPE IF EXISTS accessmode")
    op.execute("DROP TYPE IF EXISTS recordtype")
    op.execute("DROP TYPE IF EXISTS userstatus")
    op.execute("DROP TYPE IF EXISTS rolecode")
