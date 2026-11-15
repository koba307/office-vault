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
- Phase 03: `app/core/permissions.py` — pure permission logic (decide_access, check_role_permission, matrix).
- Phase 03: `app/core/security.py` — HMAC callback tokens, invite token generation/hashing.
- Phase 03: `app/services/permissions/` package (service, category, record, file, cache, invalidation, invites, context, versions, errors).
- Phase 03: `app/bot/middlewares/` (auth, permissions, throttling, audit).
- Phase 03: Middleware registration in `main.py`.
- Phase 03: `Invite` model and migration `0008_access_and_invites`.
- Phase 03: Mixed Access grants in seed (BOSS access to all categories).
- Phase 03: Role-based category visibility in `menu.py` and `start.py`.
- Phase 03: Tests for permission matrix, callback schema, invite tokens.
- Phase 04: `app/services/categories/service.py` — category CRUD, subcategories, archive, delete.
- Phase 04: `app/bot/handlers/categories.py` — category management UI.
- Phase 04: `app/bot/keyboards/pagination.py` — pagination, confirm, cancel keyboards.
- Phase 04: `app/bot/handlers/navigation.py` — navigation, back, cancel, stale callback handling.
- Phase 04: Dynamic menus, pagination, sorting, filtering, breadcrumbs in `menu.py`.
- Phase 04: Search in section, filters, pagination in `search.py`.
- Phase 04: Role-based menus in `start.py`.
- Phase 04: Security fix — `is_user_active` now properly awaited in all handlers.
- Phase 04: Tests for categories, navigation, menus (96 tests).

### Fixed

- Ambiguous SQLAlchemy foreign keys on `GroupMember`.
- Docker Compose MinIO environment variable mismatch.
- Import handler dead code.
- Ruff lint errors.
- Existing users receive their role foreign key during upgrade; new users and role changes keep `role` and `role_id` synchronized.
- SUPERADMIN cannot be assigned through the ordinary user/role UI.
- SUPERADMIN cannot be blocked through the ordinary UI.
- Seed no longer resets category/group status or SUPERADMIN status on restart.

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
