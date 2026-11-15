# DEVELOPMENT ROADMAP — OFFICE VAULT / MillionersBot

Based on `docs/TZ.md` v2.0 and `docs/DEVELOPMENT_WORKFLOW.md`.

**Rule:** one phase at a time; each phase must be implemented, tested, documented, committed, pushed, tagged, and verified before the next phase starts.

---

## Phase 01 — Project Governance & Baseline

**Status:** GITHUB_BACKUP_VERIFIED

### Objectives

- Establish strict phase-based development process.
- Record the full TZ, gap analysis, architecture, and current baseline.
- Create mandatory project management documents.
- Verify GitHub remote and baseline commit.

### Features / Components

- `docs/TZ.md` — full TZ v2.0.
- `docs/TZ-summary.md` — short version.
- `docs/DEVELOPMENT_WORKFLOW.md` — workflow rules.
- `docs/GAP-ANALYSIS.md` — current vs target.
- `DEVELOPMENT_ROADMAP.md`, `PROJECT_STATUS.md`, `CHANGELOG.md`, `ARCHITECTURE.md`, `DEVELOPMENT_LOG.md`.
- Git baseline tag.

### Files Expected

- Documentation files only; no functional code changes in this phase.

### Dependencies

- None.

### Implementation Requirements

- All documents must be accurate and consistent with the current codebase.
- No secrets in repository.

### Testing Requirements

- `git status` clean after commit.
- `git log` contains the baseline commit.
- Remote contains the commit.
- Tag exists locally and remotely.

### Acceptance Criteria

- All listed documents exist and are committed.
- GitHub push verified.
- Baseline tag `v0.1.0-phase-01` exists.
- `PROJECT_STATUS.md` marked `GITHUB_BACKUP_VERIFIED`.

### Deliverables

- Documented development baseline.

### GitHub Backup

- Commit message: `phase-01: establish project governance and baseline`
- Tag: `v0.1.0-phase-01`
- Verify remote commit and tag.

---

## Phase 02 — Database Model & Alembic Migrations

**Status:** TESTING

### Objectives

- Bring the database schema to TZ v2.0.
- Replace `Base.metadata.create_all` with Alembic migrations.
- Add missing tables, indexes, constraints.

### Features / Components

- Roles and permissions tables.
- Category/record/file permission tables.
- Tags, record versions, record files.
- Files, file versions, file access logs, file scan results.
- Recent records, user settings.
- Import/export jobs and errors.
- Security events, backups, system settings, encryption key versions, background jobs.
- Alembic configuration and initial migration.
- Seed migration for 4 categories, 4 groups, SUPERADMIN.
- Pinned dependency set in `requirements.lock`, consumed by the Docker build.

### Files Expected

- `app/database/models/__init__.py` (extend the existing model package; domain
  splitting is deferred to a separately reviewed refactor to keep this phase
  focused).
- `app/database/migrations/env.py`, `alembic.ini`, `versions/*`.
- `app/database/session.py`.
- `app/bot/main.py` (startup via Alembic).
- `requirements.lock` and `Dockerfile`.
- Tests for model metadata, seed idempotency, legacy schema validation, and migration smoke.

### Dependencies

- Phase 01.

### Implementation Requirements

- PostgreSQL only.
- UUID primary keys.
- Foreign keys and indexes.
- No destructive cascade deletes for shared files.
- Migration must be reversible where safe.

### Testing Requirements

- `alembic upgrade head` on empty DB.
- `alembic downgrade base` and re-upgrade.
- Upgrade from a database created with the prior ORM schema.
- `alembic check` reports no model/schema drift.
- Model import smoke test.
- Docker bot/worker images build from the pinned lock file.
- Existing functionality regression check.

### Acceptance Criteria

- All TZ tables exist.
- Migrations run from scratch and from current schema.
- Existing legacy users receive a non-null `role_id` during migration.
- System seed is idempotent and creates the exact four categories/groups.
- All declared Python dependencies are pinned and Docker uses the lock file.
- Seed data created.
- Tests pass.
- GitHub backup verified.

### Deliverables

- Complete DB schema and migrations.

### GitHub Backup

- Commit message: `phase-02: implement database model and alembic migrations`
- Tag: `v0.2.0-phase-02`

---

## Phase 03 — Authentication, Roles, Groups & Permission Service

**Status:** NOT_STARTED

### Objectives

- Implement Mixed Access and Default Deny at category, record, and file level.
- Add auth/throttling/audit middlewares.
- Enforce permissions on every handler/service call.

### Features / Components

- Role and permission resolution.
- Access modes: ALL_EMPLOYEES, BOSSES_ONLY, GROUP, SELECTED_USERS, PRIVATE.
- Category permissions, record permissions, file permissions.
- Group membership changes take effect immediately (no stale cache).
- User status checks on every request.
- Invite tokens for onboarding.
- Admin permission management UI.

### Files Expected

- `app/core/permissions.py`, `app/core/security.py`.
- `app/services/auth/*`, `app/services/permissions/*`.
- `app/bot/middlewares/auth.py`, `permissions.py`, `throttling.py`, `audit.py`.
- Tests: roles, groups, mixed access, revoked access.

### Dependencies

- Phase 02.

### Testing Requirements

- Unit tests for permission matrix.
- Integration tests for category/record/file access.
- Security tests: callback forgery, revoked access, blocked user.

### Acceptance Criteria

- Default Deny enforced.
- BOSS sees all working categories as required.
- EMPLOYEE read-only.
- SUPERADMIN full access.
- Access changes apply immediately.
- Tests pass.
- GitHub backup verified.

### Deliverables

- Permission service and middleware.

### GitHub Backup

- Commit message: `phase-03: implement authentication roles groups and mixed access`
- Tag: `v0.3.0-phase-03`

---

## Phase 04 — Categories, Dynamic Menus & Navigation

**Status:** NOT_STARTED

### Objectives

- Implement dynamic role-based menus and full navigation.
- Add subcategories and category management.
- Add pagination, sorting, filtering, confirmations, cancel.

### Features / Components

- EMPLOYEE / BOSS / SUPERADMIN menus per TZ.
- Dynamic category visibility by permissions.
- Category type menus.
- Subcategory CRUD.
- Breadcrumbs, back, main menu, pagination.
- Search inside section.
- Confirmation for destructive actions.
- Stale callback handling.

### Files Expected

- `app/bot/keyboards/*`.
- `app/bot/handlers/menu.py`, `navigation.py`, `categories.py`.
- `app/services/categories/*`.
- Tests for menu visibility and navigation.

### Dependencies

- Phase 03.

### Testing Requirements

- Role-based menu tests.
- Category/subcategory CRUD tests.
- Pagination tests.

### Acceptance Criteria

- Menus match TZ.
- Only permitted categories visible.
- All menu buttons work.
- No dead handlers.
- Tests pass.
- GitHub backup verified.

### Deliverables

- Complete Telegram navigation.

### GitHub Backup

- Commit message: `phase-04: implement categories dynamic menus and navigation`
- Tag: `v0.4.0-phase-04`

---

## Phase 05 — Records, Dynamic Fields, Versions & Archive

**Status:** NOT_STARTED

### Objectives

- Implement full record lifecycle and all field types.
- Add tags, attachments references, visibility, versions, optimistic locking.
- Add archive/restore/permanent delete.

### Features / Components

- Record types: CREDENTIALS, SIP, LINKS, SETTINGS, INSTRUCTIONS, FILES, OTHER.
- Field types: TEXT, LONG_TEXT, PASSWORD, SECRET, URL, EMAIL, PHONE, NUMBER, BOOLEAN, DATE, DATETIME, JSON, FILE_REFERENCE, MULTISELECT.
- Full add/edit FSM with preview and cancel.
- Tags and record_tags.
- Record versions and change history.
- Optimistic locking.
- Move record, archive, restore, permanent delete.
- Per-record access settings.

### Files Expected

- `app/bot/handlers/records.py`, `credentials.py`, `sip.py`, `links.py`, `settings.py`, `instructions.py`, `archive.py`.
- `app/bot/states/add_record.py`, `edit_record.py`, `archive.py`.
- `app/services/records/*`, `app/services/tags/*`, `app/services/archive/*`.
- Tests for record lifecycle and field types.

### Dependencies

- Phase 04.

### Testing Requirements

- CRUD tests for each record type.
- Field type validation tests.
- Version conflict test.
- Archive/restore test.

### Acceptance Criteria

- All record types and field types work.
- Secrets encrypted and masked.
- Version history recorded.
- Archive/restore/permanent delete work.
- Tests pass.
- GitHub backup verified.

### Deliverables

- Full record management.

### GitHub Backup

- Commit message: `phase-05: implement records dynamic fields versions and archive`
- Tag: `v0.5.0-phase-05`

---

## Phase 06 — Files & Storage

**Status:** NOT_STARTED

### Objectives

- Implement secure file storage, metadata, scanning, versions, access control.

### Features / Components

- Files table and metadata.
- SHA-256, MIME/magic bytes, extension checks.
- Quarantine, ClamAV scan results.
- Multiple attachments, file versions.
- Secure download with permission checks.
- File access logs.
- Size limits, path traversal and ZIP bomb protections.

### Files Expected

- `app/services/files/*`, `app/services/storage/*`.
- `app/bot/handlers/files.py`.
- `app/workers/tasks.py` scan/import tasks.
- Tests for upload/download/scan/permissions.

### Dependencies

- Phase 05.

### Testing Requirements

- Upload/download roundtrip.
- Infected file blocked.
- Unauthorized download denied.
- Versioning test.

### Acceptance Criteria

- Files stored privately in S3.
- No permanent public URLs.
- Permissions enforced.
- Audit logs written.
- Tests pass.
- GitHub backup verified.

### Deliverables

- Secure file module.

### GitHub Backup

- Commit message: `phase-06: implement secure file storage and access`
- Tag: `v0.6.0-phase-06`

---

## Phase 07 — Search, Favorites & Recent

**Status:** NOT_STARTED

### Objectives

- Permission-aware search, filters, favorites, recent history.

### Features / Components

- Search by title, description, category, type, subcategory, tags, fields, file names, SIP username, URL, record ID.
- Partial/exact/multi-word search, date filters.
- Category-scoped search.
- Favorites with access re-check.
- Recent records table and history.
- No secrets in index/history.

### Files Expected

- `app/services/search/*`, `app/services/favorites/*`, `app/services/recent/*`.
- `app/bot/handlers/search.py`, `favorites.py`, `recent.py`.
- Tests for permissions and filters.

### Dependencies

- Phase 06.

### Testing Requirements

- Search visibility tests for each role.
- Filter tests.
- Favorites revoked access test.
- Recent history test.

### Acceptance Criteria

- Search never reveals forbidden records.
- Filters work.
- Favorites/recent respect access.
- Tests pass.
- GitHub backup verified.

### Deliverables

- Search and user convenience features.

### GitHub Backup

- Commit message: `phase-07: implement permission-aware search favorites and recent`
- Tag: `v0.7.0-phase-07`

---

## Phase 08 — Import & Export

**Status:** NOT_STARTED

### Objectives

- Implement full import/export pipeline with previews, validation, background jobs, reports.

### Features / Components

- Import formats: XLSX, XLS, CSV, TSV, JSON, JSONL, XML, TXT.
- Format/encoding detection, preview, field mapping, duplicate checks.
- Transactional/idempotent import, partial errors report.
- Background import with progress.
- Export XLSX, CSV, JSON, ZIP with files and manifest.
- Field/category/type/attachment selection.
- Privileged secret export with confirmation and audit.

### Files Expected

- `app/services/import_export/*`.
- `app/bot/handlers/import_export.py`.
- `app/workers/tasks.py`.
- Tests for formats, validation, export.

### Dependencies

- Phase 07.

### Testing Requirements

- Import each supported format.
- Error report test.
- Export roundtrip.
- Secret export permission test.

### Acceptance Criteria

- All formats work.
- Reports generated.
- Secrets protected.
- Tests pass.
- GitHub backup verified.

### Deliverables

- Full import/export.

### GitHub Backup

- Commit message: `phase-08: implement import and export pipeline`
- Tag: `v0.8.0-phase-08`

---

## Phase 09 — Administration, Audit, Statistics & Notifications

**Status:** NOT_STARTED

### Objectives

- Complete admin functions, full audit, statistics, notifications.

### Features / Components

- User CRUD, invite tokens, role/group management.
- View user accessible sections.
- Full audit and record history.
- Security events.
- Statistics overall and by category.
- Notifications with permission awareness.

### Files Expected

- `app/bot/handlers/admin.py`, `users.py`, `groups.py`, `audit.py`, `statistics.py`.
- `app/services/audit/*`, `app/services/statistics/*`, `app/services/notifications/*`.
- Tests for admin and audit.

### Dependencies

- Phase 08.

### Testing Requirements

- Role management tests.
- Audit completeness tests.
- Statistics accuracy tests.

### Acceptance Criteria

- Admin functions complete.
- Audit logs complete.
- Statistics correct.
- Notifications safe.
- Tests pass.
- GitHub backup verified.

### Deliverables

- Administration and observability.

### GitHub Backup

- Commit message: `phase-09: implement administration audit statistics and notifications`
- Tag: `v0.9.0-phase-09`

---

## Phase 10 — Backup & Recovery

**Status:** NOT_STARTED

### Objectives

- Implement PostgreSQL/MinIO/config backups, encryption, external storage, restore.

### Features / Components

- Scheduled and manual backups.
- Multiple generations, integrity checks.
- Encrypted backups.
- External storage.
- Restore procedure and report.
- Telegram admin UI for backup/restore.

### Files Expected

- `app/services/backup/*`.
- `app/workers/tasks.py`.
- `app/bot/handlers/backup.py`.
- `infrastructure/backup/*`.
- Recovery tests.

### Dependencies

- Phase 09.

### Testing Requirements

- Backup creation test.
- Restore test.
- Integrity/encryption test.

### Acceptance Criteria

- Backups complete and restorable.
- External copy exists.
- Master keys protected separately.
- Tests pass.
- GitHub backup verified.

### Deliverables

- Backup and recovery module.

### GitHub Backup

- Commit message: `phase-10: implement backup and recovery`
- Tag: `v0.10.0-phase-10`

---

## Phase 11 — Security Hardening, Testing, Documentation & Production Deployment

**Status:** NOT_STARTED

### Objectives

- Final security, test coverage, documentation, production readiness.

### Features / Components

- Rate limiting, bandit, pip-audit, dependency scanning.
- Health checks, log rotation, monitoring.
- Full functional/integration/security/load/recovery tests.
- README and operations documentation.
- Production Docker Compose.
- Final release tag.

### Files Expected

- `tests/*`, `infrastructure/*`, `docs/*`, `.github/workflows/*`.
- `README.md`, `docker-compose.yml`, `Dockerfile`.

### Dependencies

- Phase 10.

### Testing Requirements

- Full test suite.
- Security audit.
- Load tests.
- Recovery tests.
- Deployment smoke test.

### Acceptance Criteria

- All TZ acceptance criteria met.
- Tests pass.
- Security audit clean.
- Documentation complete.
- Final tag created and verified.

### Deliverables

- Production-ready OFFICE VAULT.

### GitHub Backup

- Commit message: `phase-11: production readiness and final verification`
- Tag: `v1.0.0`
