# DEVELOPMENT LOG

## 2026-11-15 — Phase 03: Authentication, Roles, Groups & Permission Service

### Completed

- Phase 03 completed after Phase 02 GitHub backup verification.
- Created `app/core/permissions.py` — pure permission logic (decide_access, check_role_permission, matrix).
- Created `app/core/security.py` — HMAC callback tokens, invite token generation/hashing.
- Created `app/services/permissions/` package (service, category, record, file, cache, invalidation, invites, context, versions, errors).
- Created `app/bot/middlewares/` (auth, permissions, throttling, audit).
- Registered middlewares in `main.py`.
- Added `Invite` model and migration `0008_access_and_invites`.
- Updated seed with Mixed Access (BOSS grants on all categories).
- Updated `menu.py` and `start.py` for role-based category visibility.
- Added tests for permission matrix, callback schema, invite tokens.
- Ruff and pytest pass; Docker build passed; image has no `.env`.

### Verification

- Commit: `fe7aab0` — `phase-03: implement permission service, middlewares, mixed access and invites`.
- Pushed to `main`: remote commit matches `fe7aab0`.
- Tag `v0.3.0-phase-03` created and pushed, points to `fe7aab0`.

### Phase status

- Phase 03 marked `GITHUB_BACKUP_VERIFIED`.
- Phase 04 ready to begin.

## 2026-11-15 — Phase 02: Database Model & Alembic Migrations

### Implemented

- Expanded SQLAlchemy metadata to 37 TZ v2.0 tables, including roles and
  permissions, mixed-access relations, categories, record/file metadata,
  versions, import/export jobs, audit/security events, backups, settings, and
  background jobs.
- Added the remaining access, field, scan, job, and backup enums.
- Added seven ordered Alembic revisions (`0001`–`0007`) and async migration
  environment/configuration.
- Replaced startup `Base.metadata.create_all` with Alembic `upgrade head`.
- Added fail-closed validation before stamping a pre-Alembic database: only the
  exact supported legacy table/column set is stamped at `0001`.
- Added deterministic system roles and backfilled all existing users' non-null
  `role_id` in migration `0002`.
- Made system seed idempotent for roles, permissions, the four categories and
  groups, category VIEW grants, and configured SUPERADMIN.
- Updated new-user/role-change paths to maintain both `role` and `role_id`, and
  rejected attempts to assign SUPERADMIN through the ordinary role UI/service.
- Added `requirements.lock`; Docker now installs exact pinned versions from it.
- Added `.dockerignore` and ignored all local `.env.*` files; new Docker image
  build contexts exclude environment credentials.
- Added tests for model tables/enums, migration runner validation, lock coverage,
  seed idempotency, role FK synchronization, and SUPERADMIN assignment guard.

### Validation so far

- Fresh PostgreSQL migration to head: passed.
- `alembic check` after fresh migration: no schema drift.
- Legacy ORM-created PostgreSQL schema: validated, stamped, upgraded, seeded,
  and checked successfully.
- Existing legacy employee record: `role_id` backfilled and verified non-null.
- Repeated seed: idempotent.
- `alembic downgrade base` and re-upgrade on a dedicated empty test DB: passed.
- Partial/unknown schema: startup refuses to stamp it.
- `ruff check app tests`: passed.
- `compileall`: passed.
- `pytest` with isolated PostgreSQL test database: 12 passed.
- Alembic fresh upgrade and legacy stamp/upgrade both pass `alembic check`.
- Alembic downgrade-to-base and re-upgrade smoke test: passed on a dedicated DB.
- Docker Compose config and pinned-lock bot/worker build: passed.
- Tests in the built bot image: 11 passed, 1 skipped (the optional integration test has no `TEST_DATABASE_URL` in the image).
- New Docker image check confirms `/app/.env` is absent.
- The running compose containers were intentionally not recreated: Phase 02 is
  not yet GitHub-verified, and startup would apply schema migrations. The
  already-running images were built before `.dockerignore`; rotate exposed
  credentials and recreate containers only through a planned secure deployment.

### Current phase status

- Phase 02 marked `GITHUB_BACKUP_VERIFIED`.
- Implementation commit: `071f2bb` — `phase-02: harden migrations and seeds after audit`.
- Remote commit: `a0e4fa0` (tag `v0.2.0-phase-02`).
- Phase 03 ready to begin.

## 2026-11-15 — Phase 01: Project Governance & Baseline

### Actions

- Reviewed the existing repository history and current implementation.
- Analyzed the full TZ v2.0 (`docs/TZ.md`).
- Documented the gap between current code and TZ v2.0 (`docs/GAP-ANALYSIS.md`).
- Saved the development workflow (`docs/DEVELOPMENT_WORKFLOW.md`).
- Created the phase-based roadmap (`DEVELOPMENT_ROADMAP.md`).
- Created project status (`PROJECT_STATUS.md`), changelog (`CHANGELOG.md`), architecture (`ARCHITECTURE.md`), and this log.
- Verified current baseline:
  - Bot starts and polls Telegram.
  - Docker Compose stack runs (bot, worker, PostgreSQL, Redis, RustFS, ClamAV).
  - `pytest`: 2 passed.
  - `ruff check`: passed.
- Identified environment constraint: `minio/minio` image unavailable; `rustfs/rustfs` used as S3-compatible replacement.

### Current Blockers

- None for Phase 01.

### Completion

- Committed as `aab86cf` (`phase-01: establish project governance and baseline`).
- Pushed to `origin/main`.
- Tag `v0.1.0-phase-01` created and pushed.
- Remote commit and tag verified.
- Phase 01 marked `GITHUB_BACKUP_VERIFIED`.

### Next

- Begin Phase 02 (database model and Alembic migrations).

## 2026-11-15 — AI chat analysis

### Actions

- Re-extracted and analyzed the shared AI development chat.
- Documented the chat chronology and stopping point in `docs/AI_CHAT_ANALYSIS.md`.
- Determined that the chat ends at commit `eeb87e4` (records service + FSM for CREDENTIALS/SIP).
- The next planned chat step (record card, secret reveal with audit, search, favorites) is already implemented in commits `5c075a2`–`5d17e18`.
- Confirmed that development should continue from Phase 02, not roll back to the chat's stopping point.

## 2026-11-15 — Documentation update

### Actions

- Added a mandatory Russian-language communication rule to `docs/DEVELOPMENT_WORKFLOW.md` (section 0).
- All explanations, phase reports, plans, and user-facing responses must be in Russian.

## 2026-11-15 — Pre-phase baseline work

### Actions

- Cloned `koba307/office-vault` into `/root/MillionersBot`.
- Continued implementation:
  - Missing handlers: recent, profile, admin roles.
  - File download.
  - Record editing.
  - CSV import.
  - ARQ worker.
  - ClamAV integration.
  - Tests and linting.
- Fixed SQLAlchemy relationship ambiguity.
- Fixed Docker build and compose issues.
- Deployed the bot on the current server.
- Stopped the conflicting bot instance on Aeza.
- Pushed baseline commits to GitHub.

### Commits

- `e601b77` — continue implementation: missing handlers, file download/edit, import, worker, tests.
- `896ad1c` — remove leftover dead code in import handler.
- `3ea97bd` — add TZ document, fix Docker build, use RustFS-compatible S3 storage.
- `095d599` — add full TZ v2.0 and gap analysis.
