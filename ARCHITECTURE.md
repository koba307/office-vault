# ARCHITECTURE — OFFICE VAULT / MillionersBot

## 1. Current Architecture

```
Telegram User
    │
    ▼
Telegram Bot API
    │
    ▼
aiogram 3 Bot (app/bot)
    │
    ├── handlers/        Telegram handlers (start, menu, records, admin, files, search, favorites, recent, profile)
    ├── states/          FSM states
    └── main.py          Startup, DB init, seed, polling
    │
    ▼
Services (app/services)
    ├── auth/            Users, groups, permissions, admin operations
    ├── records/         Record CRUD, archive, delete, update
    ├── files/           S3 upload/download, optional ClamAV
    ├── import_export/   CSV import/export
    ├── search/          Basic search
    ├── favorites/       Favorites
    └── recent/          Recent records (derived)
    │
    ▼
PostgreSQL (SQLAlchemy 2 async) + Redis (FSM) + S3-compatible storage (RustFS) + ClamAV
    │
    ▼
ARQ worker (app/workers)
    ├── backup_records   CSV backup to S3
    └── cleanup_archived Hard-delete archived records after 30 days
```

## 2. Current Data Model

- `users` — Telegram users, role enum, status.
- `groups` — system groups.
- `group_members` — user-group membership.
- `categories` — 4 root categories.
- `records` — universal records.
- `record_fields` — dynamic fields (TEXT / PASSWORD).
- `audit_logs` — audit events.
- `favorites` — user favorites.

## 3. Target Architecture (TZ v2.0)

```
Telegram User
    │
    ▼
aiogram Bot
    │
    ▼
Middlewares (auth, permissions, throttling, audit)
    │
    ▼
Handlers → Services → Repositories
    │
    ├── Permission Service (Mixed Access)
    ├── Category Service
    ├── Record Service (+ versions, tags, access)
    ├── File Service (+ metadata, scan, versions)
    ├── Search Service (permission-aware)
    ├── Import/Export Service (jobs, reports)
    ├── Audit Service (events, history)
    ├── Backup Service (PostgreSQL, S3, restore)
    └── Notification Service
    │
    ▼
PostgreSQL + Redis + S3/MinIO + ClamAV
    │
    ▼
ARQ Workers (import, scan, backup, cleanup, notifications)
```

## 4. Design Rules

- Telegram is the only user/admin interface.
- No web panel or browser frontend.
- Handlers contain no business logic; they call services.
- Services enforce permissions on every operation.
- Default Deny.
- Secrets encrypted with AES-256-GCM; never logged.
- Files stored privately in S3, never only as Telegram file_id.
- Every sensitive action is audited.
- One phase at a time; each phase is tested, documented, committed, tagged.

## 5. Infrastructure

- Docker Compose: `bot`, `worker`, `postgres`, `redis`, `minio` (RustFS-compatible S3), `clamav`.
- PostgreSQL 16.
- Redis 7.
- S3-compatible object storage.
- ClamAV (optional but enabled in compose).
- ARQ for background jobs.

## 6. Security Boundaries

- PostgreSQL, Redis, and S3 are not exposed publicly.
- Only bot long-polling requires outbound internet.
- Master encryption key is provided via environment and must not be stored with data.
- `.env` is ignored by Git; `.env.example` contains placeholders only.
- Permissions are always checked server-side; callback data is not proof of access.
