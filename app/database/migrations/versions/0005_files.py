"""files

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-15

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    scanstatus = postgresql.ENUM(name="scanstatus", create_type=False)

    op.create_table(
        "files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("original_name", sa.String(512), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("bucket", sa.String(128), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("extension", sa.String(32), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("scan_status", scanstatus, nullable=False),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], name="fk_files_uploaded_by"),
        sa.UniqueConstraint("storage_key", name="uq_files_storage_key"),
    )
    op.create_index("ix_files_sha256", "files", ["sha256"])

    op.create_table(
        "file_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("scan_status", scanstatus, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["file_id"], ["files.id"], name="fk_file_versions_file", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("file_id", "version", name="uq_file_version"),
    )

    op.create_table(
        "record_files",
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["record_id"], ["records.id"], name="fk_record_files_record", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["file_id"], ["files.id"], name="fk_record_files_file", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("record_id", "file_id", name="pk_record_files"),
    )

    op.create_table(
        "file_access_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["file_id"], ["files.id"], name="fk_file_access_logs_file", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_file_access_logs_user"),
    )
    op.create_index("ix_file_access_logs_file", "file_access_logs", ["file_id"])

    op.create_table(
        "file_scan_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scanner", sa.String(64), nullable=False),
        sa.Column("status", scanstatus, nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["file_id"], ["files.id"], name="fk_file_scan_results_file", ondelete="CASCADE"
        ),
    )


def downgrade() -> None:
    op.drop_table("file_scan_results")
    op.drop_index("ix_file_access_logs_file", table_name="file_access_logs")
    op.drop_table("file_access_logs")
    op.drop_table("record_files")
    op.drop_table("file_versions")
    op.drop_index("ix_files_sha256", table_name="files")
    op.drop_table("files")
