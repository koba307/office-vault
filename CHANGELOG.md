# CHANGELOG

All notable changes to this project are documented here.

## [Unreleased]

### Added

- Full TZ v2.0 (`docs/TZ.md`).
- TZ summary (`docs/TZ-summary.md`).
- Development workflow (`docs/DEVELOPMENT_WORKFLOW.md`).
- Gap analysis (`docs/GAP-ANALYSIS.md`).
- Development roadmap (`DEVELOPMENT_ROADMAP.md`).
- Project status (`PROJECT_STATUS.md`).
- Architecture document (`ARCHITECTURE.md`).
- Development log (`DEVELOPMENT_LOG.md`).
- Missing bot handlers: recent, profile, admin roles/groups view.
- File download handler.
- Record edit FSM (title/description).
- CSV import handler.
- ARQ worker with backup and cleanup tasks.
- Optional ClamAV scanning.
- Basic tests for encryption.
- Docker build fixes (hatchling, README copy).
- RustFS as S3-compatible storage replacement for unavailable MinIO image.
- Complete PostgreSQL model set for TZ v2.0 (roles/permissions, category and record access, tags, versions, files, jobs, audit and system tables).
- Alembic async environment and seven ordered migrations.
- Strict legacy-schema validation before stamping pre-Alembic databases.
- Idempotent system seed for roles, permissions, categories, groups and SUPERADMIN.
- Pinned Python dependency set in `requirements.lock`; Docker now installs from the lock file.
- `.dockerignore` to keep environment files and local artifacts out of newly built images.
- Database schema, migration-runner and dependency-lock tests.

### Fixed

- Ambiguous SQLAlchemy foreign keys on `GroupMember`.
- Docker Compose MinIO environment variable mismatch.
- Import handler dead code.
- Ruff lint errors.
- Existing users receive their role foreign key during upgrade; new users and role changes keep `role` and `role_id` synchronized.
- SUPERADMIN cannot be assigned through the ordinary user/role UI.

## [0.1.0] — 2026-11-14

### Added

- Initial foundation: Docker Compose, PostgreSQL, Redis, MinIO, aiogram 3.
- Users, groups, categories, roles.
- 4 system categories and groups.
- SUPERADMIN bootstrap.
- Main menu and category navigation.
- Universal records, dynamic fields, AES-256-GCM.
- Search, favorites, archive/delete.
- Admin user management.
- File upload, CSV export.
- Oracle Cloud deployment guide.
