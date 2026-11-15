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
- Phase 05: `app/services/records/fields.py` — all 14 field types, validation, encryption, masking.
- Phase 05: Full FSM for adding/editing records with all stages.
- Phase 05: `app/services/records/versions.py` — version snapshots, restore, optimistic locking.
- Phase 05: `app/services/records/tags.py` — tag CRUD, add/remove from records.
- Phase 05: `app/bot/handlers/archive.py` — archive management.
- Phase 05: Record move, restore, permanent delete, attachments.
- Phase 05: Tests for record fields, versions, tags, lifecycle (289 tests).
- Phase 06: `app/services/files/metadata.py` — SHA-256, MIME detection, extension validation, filename sanitization, size check.
- Phase 06: `app/services/files/quarantine.py` — quarantine, release, is_quarantined.
- Phase 06: `app/services/files/security.py` — ZIP bomb protection, path traversal, magic bytes validation.
- Phase 06: `app/services/files/versions.py` — file versions, restore, replace.
- Phase 06: `app/services/files/access.py` — file access checks, download, upload, audit logging.
- Phase 06: Updated `app/services/files/service.py` — full upload cycle with security, quarantine, ClamAV, S3, metadata.
- Phase 06: Updated `app/bot/handlers/files.py` — secure download, metadata card, delete with confirmation, restore.
- Phase 06: Updated `app/bot/handlers/archive.py` — file archive management.
- Phase 06: Tests for file metadata, quarantine, versions, access, security, scan (505 tests).
- Phase 06: Security fix — `is_user_active` now properly awaited in all handlers.
- Phase 04: Security fix — `is_user_active` now properly awaited in all handlers.
- Phase 07: `app/services/search/service.py` — permission-aware search with filters.
- Phase 07: `app/bot/handlers/search.py` — search in section, filters, pagination.
- Phase 07: `app/services/favorites/service.py` — favorites with access re-check, pagination.
- Phase 07: `app/bot/handlers/favorites.py` — favorites list, pagination, add/remove.
- Phase 07: `app/services/recent/service.py` — recent records with history, access check.
- Phase 07: `app/bot/handlers/recent.py` — recent list, pagination, clear history.
- Phase 07: Tests for search, favorites, recent (583 tests).

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
