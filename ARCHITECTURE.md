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
    └── main.py          Startup, Alembic migrations, seed, polling
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

- **Пользователи и права:** `users`, `roles`, `permissions`, `role_permissions`,
  `user_permissions`, `groups`, `group_members`, `user_group_permissions`.
- **Категории:** `categories`, `category_permissions`,
  `category_group_permissions`, `category_user_permissions`.
- **Записи:** `records`, `record_fields`, `record_permissions`,
  `record_group_permissions`, `record_user_permissions`, `tags`, `record_tags`,
  `record_versions`.
- **Файлы:** `files`, `file_versions`, `record_files`, `file_access_logs`,
  `file_scan_results`.
- **Пользовательские функции:** `favorites`, `recent_records`, `user_settings`.
- **Импорт/экспорт:** `import_jobs`, `import_errors`, `export_jobs`.
- **Аудит:** `audit_logs`, `security_events`.
- **Инфраструктура:** `backups`, `system_settings`, `encryption_key_versions`,
  `background_jobs`.

Схема управляется Alembic (`app/database/migrations`). Существующая до Alembic
база автоматически получает baseline stamp только после проверки точного набора
таблиц и колонок.

## 3. Current Services (Phase 03)

- `app/services/auth/` — users, groups, permissions, admin operations.
- `app/services/permissions/` — PermissionService, category/record/file checks, cache, invalidation, invites.
- `app/services/records/` — record CRUD, archive, delete, update.
- `app/services/files/` — S3 upload/download, optional ClamAV.
- `app/services/import_export/` — CSV import/export.
- `app/services/search/` — basic search.
- `app/services/favorites/` — favorites.
- `app/services/recent/` — recent records.
- `app/core/permissions.py` — pure permission logic.
- `app/core/security.py` — HMAC callback tokens, invite token generation/hashing.
- `app/bot/middlewares/` — auth, permissions, throttling, audit.

## 4. Target Architecture (TZ v2.0)

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

## 5. Design Rules

- Telegram is the only user/admin interface.
- No web panel or browser frontend.
- Handlers contain no business logic; they call services.
- Services enforce permissions on every operation.
- Default Deny.
- Secrets encrypted with AES-256-GCM; never logged.
- Files stored privately in S3, never only as Telegram file_id.
- Every sensitive action is audited.
- One phase at a time; each phase is tested, documented, committed, tagged.

## 6. Infrastructure

- Docker Compose: `bot`, `worker`, `postgres`, `redis`, `minio` (RustFS-compatible S3), `clamav`.
- PostgreSQL 16.
- Redis 7.
- S3-compatible object storage.
- ClamAV (optional but enabled in compose).
- ARQ for background jobs.
- Python dependencies are pinned in `requirements.lock`; Docker builds install from that lock.

## 7. Security Boundaries

- PostgreSQL, Redis, and S3 are not exposed publicly.
- Only bot long-polling requires outbound internet.
- Master encryption key is provided via environment and must not be stored with data.
- `.env` is ignored by Git; `.env.example` contains placeholders only.
- `.dockerignore` excludes `.env` from new image build contexts.
- Permissions are always checked server-side; callback data is not proof of access.
