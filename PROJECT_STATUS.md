# PROJECT STATUS — OFFICE VAULT / MillionersBot

**Last updated:** 2026-11-15  
**Current phase:** Phase 05 — Records, Dynamic Fields, Versions & Archive  
**Phase status:** NOT_STARTED
**Overall status:** Phase 01, 02, 03 and 04 verified; Phase 05 ready to begin.

## Current Phase

| Field | Value |
|---|---|
| Phase | 03 — Authentication, Roles, Groups & Permission Service |
| Status | NOT_STARTED |
| Started | 2026-11-15 |
| Depends on | Phase 01 (verified) |
| Next phase | 03 — Authentication, Roles, Groups & Permission Service |

## Completed Phases

| Phase | Name | Status |
|---|---|---|
| 01 | Project Governance & Baseline | GITHUB_BACKUP_VERIFIED |
| 02 | Database Model & Alembic Migrations | GITHUB_BACKUP_VERIFIED |
| 03 | Authentication, Roles, Groups & Permission Service | GITHUB_BACKUP_VERIFIED |
| 04 | Categories, Dynamic Menus & Navigation | GITHUB_BACKUP_VERIFIED |

## Remaining Phases

| Phase | Name | Status |
|---|---|---|
| 05 | Records, Dynamic Fields, Versions & Archive | NOT_STARTED |
| 06 | Files & Storage | NOT_STARTED |
| 07 | Search, Favorites & Recent | NOT_STARTED |
| 08 | Import & Export | NOT_STARTED |
| 09 | Administration, Audit, Statistics & Notifications | NOT_STARTED |
| 10 | Backup & Recovery | NOT_STARTED |
| 11 | Security Hardening, Testing & Production Deployment | NOT_STARTED |

## Implementation Progress

### Implemented baseline

- Docker Compose: bot, worker, PostgreSQL, Redis, S3-compatible storage (RustFS), ClamAV.
- aiogram 3 bot, `/start`, main menu, 4 categories, 7 record types.
- Users, groups, roles (enum), block/unblock, admin add user.
- Basic Permission Service (Default Deny by group).
- Records, dynamic fields (TEXT / PASSWORD), AES-256-GCM secrets.
- Record card, secret reveal with audit.
- Search (basic), favorites, recent (derived).
- Archive/soft delete, edit title/description.
- File upload/download, CSV import/export.
- Audit log table, partial logging.
- ARQ worker: CSV backup and archived cleanup.
- Basic encryption tests.
- 37 SQLAlchemy tables matching the Phase 02 data-model scope.
- Seven Alembic revisions; bot startup now applies migrations instead of `create_all`.
- Strict and verified upgrade path for the exact pre-Alembic schema; partial/unknown schemas fail closed.
- Idempotent seed for role/permission catalog, four categories/groups, category VIEW grants, and SUPERADMIN.
- Legacy user role backfill to non-null `users.role_id`; create/change-user-role flows keep `role` and `role_id` synchronized.
- `requirements.lock` with pinned versions; Docker installs from the lock file.

### Not yet compliant with TZ v2.0

See `docs/GAP-ANALYSIS.md`.

## Test Results

| Test suite | Result | Notes |
|---|---|---|
| `pytest` with isolated PostgreSQL `TEST_DATABASE_URL` | 12 passed | Models, migration-runner, seed-idempotency, lock and encryption tests |
| `ruff check app tests` | passed | No lint errors |
| `compileall` | passed | Python source compiles |
| Alembic fresh upgrade + `check` | passed | No model/schema drift |
| Legacy schema stamp + upgrade + `check` | passed | Existing employee role backfilled |
| Alembic downgrade/upgrade roundtrip | passed | Dedicated empty test database |
| Docker Compose config | passed | Configuration validates |
| Docker build | passed | bot + worker install pinned versions from `requirements.lock` |
| Image secret check | passed | Newly built image does not contain `.env` |
| Tests in built bot image | 11 passed, 1 skipped | Integration seed test skips without `TEST_DATABASE_URL` |
| Telegram conflict | resolved | Old Aeza instance stopped |

## GitHub State

| Field | Value |
|---|---|
| Repository | `koba307/office-vault` |
| Branch | `main` |
| Latest verified phase commit | `71fe89f` |
| Latest remote commit | `71fe89f` |
| Latest verified tag | `v0.4.0-phase-04` |

## Known Issues

- TZ v2.0 has many unimplemented features (see gap analysis).
- Permission model does not support record/file-level access (Phase 03 in progress).
- No full test suite.
- No backup/restore for PostgreSQL/MinIO.
- Secrets are sent in ordinary Telegram messages.
- MinIO image unavailable in this environment; RustFS is used as S3-compatible replacement.
- Previously disclosed bot/GitHub/SSH credentials must be rotated. New image builds exclude `.env`, but running containers were not recreated.


## Next Planned Actions

1. Phase 03 in progress: implement Mixed Access, middleware, record/file permissions.
2. Rotate previously disclosed credentials and recreate containers via planned deployment.
3. Complete Phase 03: tests, docs, commit, push, tag, verify.
