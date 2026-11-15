"""access and invites

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-15

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    rolecode = postgresql.ENUM(name="rolecode", create_type=False)

    op.create_table(
        "invites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("role", rolecode, nullable=False),
        sa.Column("group_codes", postgresql.JSONB(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_invites_created_by"),
        sa.ForeignKeyConstraint(["used_by"], ["users.id"], name="fk_invites_used_by"),
        sa.UniqueConstraint("token_hash", name="uq_invites_token_hash"),
    )
    op.create_index("ix_invites_expires_at", "invites", ["expires_at"])
    op.create_index("ix_invites_used_at", "invites", ["used_at"])


def downgrade() -> None:
    op.drop_index("ix_invites_used_at", table_name="invites")
    op.drop_index("ix_invites_expires_at", table_name="invites")
    op.drop_table("invites")
