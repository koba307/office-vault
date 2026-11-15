# PROJECT STATUS — OFFICE VAULT / MillionersBot

**Last updated:** 2026-11-15  
**Current phase:** Phase 02 — Database Model & Alembic Migrations  
**Phase status:** NOT_STARTED  
**Overall status:** Phase 01 verified; TZ v2.0 compliance in progress.

## Current Phase

| Field | Value |
|---|---|
| Phase | 02 — Database Model & Alembic Migrations |
| Status | NOT_STARTED |
| Started | — |
| Depends on | Phase 01 (verified) |
| Next phase | 03 — Authentication, Roles, Groups & Permission Service |

## Completed Phases

| Phase | Name | Status |
|---|---|---|
| 01 | Project Governance & Baseline | GITHUB_BACKUP_VERIFIED |

## Remaining Phases

| Phase | Name | Status |
|---|---|---|
| 02 | Database Model & Alembic Migrations | NOT_STARTED |
| 03 | Authentication, Roles, Groups & Permission Service | NOT_STARTED |
| 04 | Categories, Dynamic Menus & Navigation | NOT_STARTED |
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

### Not yet compliant with TZ v2.0

See `docs/GAP-ANALYSIS.md`.

## Test Results

| Test suite | Result | Notes |
|---|---|---|
| `pytest` | 2 passed | Encryption roundtrip only |
| `ruff check` | passed | No lint errors |
| Docker build | passed | bot + worker |
| Bot startup | passed | Polling works |
| Telegram conflict | resolved | Old Aeza instance stopped |

## GitHub State

| Field | Value |
|---|---|
| Repository | `koba307/office-vault` |
| Branch | `main` |
| Latest verified phase commit | `aab86cf` |
| Latest remote commit | `aab86cf` |
| Latest verified tag | `v0.1.0-phase-01` |

## Known Issues

- TZ v2.0 has many unimplemented features (see gap analysis).
- No Alembic migrations yet (`create_all`).
- Permission model does not support record/file-level access.
- No full test suite.
- No backup/restore for PostgreSQL/MinIO.
- BOSS does not automatically see all working categories.
- `/start` does not show employee/boss menu.
- Secrets are sent in ordinary Telegram messages.
- MinIO image unavailable in this environment; RustFS is used as S3-compatible replacement.

## Next Planned Actions

1. Start Phase 02: database model and Alembic migrations.
2. Add all missing TZ tables and indexes.
3. Configure Alembic and initial migration.
4. Run migration smoke tests and regression tests.
5. Commit, push, tag, and verify Phase 02.
