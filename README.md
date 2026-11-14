# OFFICE VAULT

Корпоративная система хранения информации и управления офисными данными через Telegram-бота.

**Версия:** 0.1.0 (фундамент по ТЗ v2.0)  
**Интерфейс:** исключительно Telegram  
**Платформа:** Linux VPS + Docker Compose

## Быстрый старт

### 1. Клонировать и настроить

```bash
git clone https://github.com/koba307/office-vault.git
cd office-vault
cp .env.example .env
# Отредактировать .env: BOT_TOKEN, SUPERADMIN_TELEGRAM_ID, пароли, MASTER_ENCRYPTION_KEY
```

### 2. Запустить

```bash
docker compose up -d --build
docker compose logs -f bot
```

### 3. Инициализация

При первом запуске бот автоматически:
- Создаёт таблицы (Alembic)
- Создаёт 4 системные категории: Холодка, 2й Этап, Клоузеры, Общее
- Создаёт системные группы
- Регистрирует SUPERADMIN по Telegram ID из `.env`

## Архитектура

```
Telegram User → aiogram Bot → Permission Service → PostgreSQL + MinIO + Redis
                                      ↓
                               Background Workers (ARQ)
```

## Этапы разработки (по ТЗ)

- [x] Этап 1: Фундамент (Docker, PostgreSQL, Redis, MinIO, конфиг, миграции)
- [x] Этап 2: Пользователи, роли, группы, Permission Service, аудит (базовый)
- [x] Этап 3: Основной интерфейс (/start, главное меню, 4 категории)
- [ ] Этап 4–10: записи, файлы, поиск, импорт/экспорт, администрирование, тесты

## Структура

См. раздел 23 ТЗ. Handlers вызывают сервисы, сервисы — репозитории.

## Безопасность

- Default Deny
- AES-256-GCM для секретов
- Проверка прав на каждый запрос
- Файлы только в MinIO (не Telegram file_id как единственное хранилище)
- Аудит всех действий

## Лицензия

Проприетарная (внутренний корпоративный проект).
