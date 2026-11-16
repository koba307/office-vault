"""max accounts

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-16

"""
from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "max_account_issues",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_max_account_issues_file_id", "max_account_issues", ["file_id"])
    op.create_index("ix_max_account_issues_user_id", "max_account_issues", ["user_id"])
    op.create_foreign_key(
        "fk_max_account_issues_file_id",
        "max_account_issues",
        "files",
        ["file_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_max_account_issues_user_id",
        "max_account_issues",
        "users",
        ["user_id"],
        ["id"],
    )

    bind = op.get_bind()
    parent = bind.execute(
        sa.text("SELECT id FROM categories WHERE code = 'second_stage'")
    ).scalar_one_or_none()
    if parent is None:
        # Корневой раздел «2й Этап» ещё не создан (например, пустая база) —
        # категорию MAX добавит сид при первом старте.
        return
    existing = bind.execute(sa.text("SELECT id FROM categories WHERE code = 'max'")).scalar_one_or_none()
    if existing is None:
        category_id = uuid4()
        bind.execute(
            sa.text(
                "INSERT INTO categories (id, parent_id, code, name, category_type, sort_order, status, created_at, updated_at) "
                "VALUES (:id, :parent, 'max', 'MAX', 'subcategory', 90, 'ACTIVE', now(), now())"
            ),
            {"id": str(category_id), "parent": str(parent)},
        )
        bind.execute(
            sa.text(
                "INSERT INTO category_permissions (category_id, access_mode, created_at, updated_at) "
                "VALUES (:cid, 'ALL_EMPLOYEES', now(), now())"
            ),
            {"cid": str(category_id)},
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM category_permissions WHERE category_id IN (SELECT id FROM categories WHERE code = 'max')"))
    bind.execute(sa.text("DELETE FROM categories WHERE code = 'max'"))
    op.drop_constraint("fk_max_account_issues_user_id", "max_account_issues", type_="foreignkey")
    op.drop_constraint("fk_max_account_issues_file_id", "max_account_issues", type_="foreignkey")
    op.drop_index("ix_max_account_issues_user_id", table_name="max_account_issues")
    op.drop_constraint("uq_max_account_issues_file_id", "max_account_issues", type_="unique")
    op.drop_table("max_account_issues")
