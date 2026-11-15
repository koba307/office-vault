"""user features, import and export

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-15

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    jobstatus = postgresql.ENUM(name="jobstatus", create_type=False)

    op.create_table(
        "recent_records",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("viewed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_recent_records_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["record_id"], ["records.id"], name="fk_recent_records_record", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "record_id", name="pk_recent_records"),
    )

    op.create_table(
        "user_settings",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_settings_user", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_settings"),
    )

    op.create_table(
        "import_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", jobstatus, nullable=False),
        sa.Column("source_format", sa.String(16), nullable=True),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("success_rows", sa.Integer(), nullable=False),
        sa.Column("failed_rows", sa.Integer(), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("report", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_import_jobs_user"),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], name="fk_import_jobs_category"),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name="fk_import_jobs_file"),
    )
    op.create_index("ix_import_jobs_user", "import_jobs", ["user_id", "status"])

    op.create_table(
        "import_errors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("raw_data", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["job_id"], ["import_jobs.id"], name="fk_import_errors_job", ondelete="CASCADE"
        ),
    )

    op.create_table(
        "export_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", jobstatus, nullable=False),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("filters", postgresql.JSONB(), nullable=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_export_jobs_user"),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], name="fk_export_jobs_category"),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name="fk_export_jobs_file"),
    )
    op.create_index("ix_export_jobs_user", "export_jobs", ["user_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_export_jobs_user", table_name="export_jobs")
    op.drop_table("export_jobs")
    op.drop_table("import_errors")
    op.drop_index("ix_import_jobs_user", table_name="import_jobs")
    op.drop_table("import_jobs")
    op.drop_table("user_settings")
    op.drop_table("recent_records")
