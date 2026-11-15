"""audit and system tables

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-15

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    jobstatus = postgresql.ENUM(name="jobstatus", create_type=False)
    backupkind = postgresql.ENUM(name="backupkind", create_type=False)

    op.add_column("audit_logs", sa.Column("correlation_id", sa.String(64), nullable=True))
    op.add_column("audit_logs", sa.Column("telegram_id", sa.BigInteger(), nullable=True))
    op.add_column("audit_logs", sa.Column("role", sa.String(32), nullable=True))
    op.add_column("audit_logs", sa.Column("ip_address", sa.String(64), nullable=True))
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_correlation", "audit_logs", ["correlation_id"])

    op.create_table(
        "security_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_security_events_user"),
    )
    op.create_index("ix_security_events_type", "security_events", ["event_type", "created_at"])

    op.create_table(
        "backups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", backupkind, nullable=False),
        sa.Column("status", jobstatus, nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("encrypted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name="fk_system_settings_updated_by"),
        sa.PrimaryKeyConstraint("key", name="pk_system_settings"),
    )

    op.create_table(
        "encryption_key_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key_fingerprint", sa.String(64), nullable=False),
        sa.Column("algorithm", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_encryption_key_versions"),
        sa.UniqueConstraint("key_fingerprint", name="uq_encryption_key_fingerprint"),
    )

    op.create_table(
        "background_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_type", sa.String(64), nullable=False),
        sa.Column("status", jobstatus, nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_background_jobs_type_status", "background_jobs", ["job_type", "status"])


def downgrade() -> None:
    op.drop_index("ix_background_jobs_type_status", table_name="background_jobs")
    op.drop_table("background_jobs")
    op.drop_table("encryption_key_versions")
    op.drop_table("system_settings")
    op.drop_table("backups")
    op.drop_index("ix_security_events_type", table_name="security_events")
    op.drop_table("security_events")
    op.drop_index("ix_audit_logs_correlation", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_column("audit_logs", "ip_address")
    op.drop_column("audit_logs", "role")
    op.drop_column("audit_logs", "telegram_id")
    op.drop_column("audit_logs", "correlation_id")
