"""registration requests

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-16

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE TYPE registrationrequeststatus AS ENUM ('PENDING', 'APPROVED', 'REJECTED')")
    op.create_table(
        "registration_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("role_code", sa.String(64), nullable=False),
        sa.Column("status", postgresql.ENUM("PENDING", "APPROVED", "REJECTED", name="registrationrequeststatus", create_type=False), nullable=False, server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_registration_requests_telegram_id", "registration_requests", ["telegram_id"])
    op.create_foreign_key(
        "fk_registration_requests_decided_by",
        "registration_requests",
        "users",
        ["decided_by"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_registration_requests_decided_by", "registration_requests", type_="foreignkey")
    op.drop_index("ix_registration_requests_telegram_id", table_name="registration_requests")
    op.drop_table("registration_requests")
    op.execute("DROP TYPE IF EXISTS registrationrequeststatus")
