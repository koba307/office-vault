# PROJECT STATUS — OFFICE VAULT / MillionersBot

**Last updated:** 2026-11-15  
**Current phase:** Phase 01 — Project Governance & Baseline  
**Phase status:** IN_PROGRESS  
**Overall status:** Foundation implemented; TZ v2.0 compliance in progress.

## Current Phase

| Field | Value |
|---|---|
| Phase | 01 — Project Governance & Baseline |
| Status | IN_PROGRESS |
| Started | 2026-11-15 |
| Depends on | — |
| Next phase | 02 — Database Model & Alembic Migrations |

## Completed Phases

| Phase | Name | Status |
|---|---|---|
| 01 | Project Governance & Baseline | IN_PROGRESS |

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
| Latest local commit | `095d599` |
| Latest remote commit | `095d599` |
| Latest verified tag | — (Phase 01 tag pending) |

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

1. Finish Phase 01 documents.
2. Commit and push Phase 01.
3. Create and verify tag `v0.1.0-phase-01`.
4. Start Phase 02: database model and Alembic migrations.
