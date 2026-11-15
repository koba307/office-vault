"""records extensions

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-15

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    accessmode = postgresql.ENUM(name="accessmode", create_type=False)

    op.add_column("records", sa.Column("subcategory_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_records_subcategory", "records", "categories", ["subcategory_id"], ["id"]
    )
    op.create_index("ix_records_subcategory_id", "records", ["subcategory_id"])
    op.create_index("ix_records_category_type_status", "records", ["category_id", "record_type", "status"])

    op.create_table(
        "tags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("color", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_tags_name"),
    )

    op.create_table(
        "record_tags",
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tag_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["record_id"], ["records.id"], name="fk_record_tags_record", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"], ["tags.id"], name="fk_record_tags_tag", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("record_id", "tag_id", name="pk_record_tags"),
    )

    op.create_table(
        "record_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("changed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("change_description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["record_id"], ["records.id"], name="fk_record_versions_record", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"], name="fk_record_versions_changed_by"),
        sa.UniqueConstraint("record_id", "version", name="uq_record_version"),
    )
    op.create_table(
        "record_permissions",
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("access_mode", accessmode, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["record_id"], ["records.id"], name="fk_record_permissions_record", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("record_id", name="pk_record_permissions"),
    )

    op.create_table(
        "record_group_permissions",
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["record_id"], ["records.id"], name="fk_rgp_record", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], name="fk_rgp_group", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["permission_id"], ["permissions.id"], name="fk_rgp_permission", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "record_id", "group_id", "permission_id", name="pk_record_group_permissions"
        ),
    )

    op.create_table(
        "record_user_permissions",
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["record_id"], ["records.id"], name="fk_rup_record", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_rup_user", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["permission_id"], ["permissions.id"], name="fk_rup_permission", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "record_id", "user_id", "permission_id", name="pk_record_user_permissions"
        ),
    )


def downgrade() -> None:
    op.drop_table("record_user_permissions")
    op.drop_table("record_group_permissions")
    op.drop_table("record_permissions")
    op.drop_table("record_versions")
    op.drop_table("record_tags")
    op.drop_table("tags")
    op.drop_index("ix_records_category_type_status", table_name="records")
    op.drop_index("ix_records_subcategory_id", table_name="records")
    op.drop_constraint("fk_records_subcategory", "records", type_="foreignkey")
    op.drop_column("records", "subcategory_id")
