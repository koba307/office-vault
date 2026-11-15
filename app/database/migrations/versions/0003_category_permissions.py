"""category permissions

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-15

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    accessmode = postgresql.ENUM(name="accessmode", create_type=False)

    op.create_table(
        "category_permissions",
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("access_mode", accessmode, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["category_id"], ["categories.id"], name="fk_category_permissions_category", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("category_id", name="pk_category_permissions"),
    )

    op.create_table(
        "category_group_permissions",
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name="fk_cgp_category",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], name="fk_cgp_group", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["permission_id"], ["permissions.id"], name="fk_cgp_permission", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "category_id", "group_id", "permission_id", name="pk_category_group_permissions"
        ),
    )

    op.create_table(
        "category_user_permissions",
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["category_id"], ["categories.id"], name="fk_cup_category", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_cup_user", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["permission_id"], ["permissions.id"], name="fk_cup_permission", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "category_id", "user_id", "permission_id", name="pk_category_user_permissions"
        ),
    )


def downgrade() -> None:
    op.drop_table("category_user_permissions")
    op.drop_table("category_group_permissions")
    op.drop_table("category_permissions")
