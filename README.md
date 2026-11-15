# OFFICE VAULT

Корпоративная система хранения информации и управления офисными данными через Telegram-бота.

**Версия:** 0.1.0 (реализация по ТЗ v2.0)  
**Интерфейс:** исключительно Telegram  
**Платформа:** Linux VPS + Docker Compose

Документы:

- Полное ТЗ: [`docs/TZ.md`](docs/TZ.md)
- Краткая версия: [`docs/TZ-summary.md`](docs/TZ-summary.md)
- Инструкции разработки: [`docs/DEVELOPMENT_WORKFLOW.md`](docs/DEVELOPMENT_WORKFLOW.md)
- Анализ пробелов и план: [`docs/GAP-ANALYSIS.md`](docs/GAP-ANALYSIS.md)
- План разработки: [`DEVELOPMENT_ROADMAP.md`](DEVELOPMENT_ROADMAP.md)
- Текущий статус: [`PROJECT_STATUS.md`](PROJECT_STATUS.md)

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

Docker-образ устанавливает зависимости из зафиксированного `requirements.lock`.
Для локальной разработки используйте ту же версию зависимостей:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pytest -q
.venv/bin/ruff check app tests
```

При изменении зависимостей обновляйте `pyproject.toml` и `requirements.lock`
в одном коммите.

### 3. Инициализация

При первом запуске бот автоматически:
- Применяет миграции Alembic до `head`
- Создаёт 4 системные категории: Холодка, 2й Этап, Клоузеры, Общее
- Создаёт системные группы
- Создаёт системные роли и права
- Регистрирует SUPERADMIN по Telegram ID из `.env`

Для уже существующей базы без Alembic бот проверяет структуру старой схемы и
помечает её revision `0001` только при точном совпадении. Если схема неполная
или содержит неизвестные таблицы/колонки, запуск останавливается — сначала
сделайте резервную копию и разберите расхождения вручную.

Ручное управление миграциями из корня проекта:

```bash
alembic current
alembic upgrade head
```

`alembic downgrade` используйте только на тестовой базе или при наличии
проверенной резервной копии.

## Архитектура

```
Telegram User → aiogram Bot → Permission Service → PostgreSQL + Redis + S3 (RustFS/MinIO)
                                      ↓
                               Background Workers (ARQ)
```

## Этапы разработки (по ТЗ)

- [x] Этап 1: Фундамент (Docker, PostgreSQL, Redis, S3-хранилище, конфиг)
- [ ] Этап 2: Модели данных, миграции Alembic и системные seed-данные
- [ ] Этап 3: Пользователи, роли, группы, Permission Service, аудит
- [ ] Этапы 4–11: интерфейс, записи, файлы, поиск, импорт/экспорт, администрирование, безопасность и приёмочные тесты

Фактический статус по фазам ведётся в [`PROJECT_STATUS.md`](PROJECT_STATUS.md).

## Проверки для разработчиков

Быстрые unit-тесты и линтинг:

```bash
pytest -q
ruff check app tests
```

Для интеграционного теста seed-функции укажите отдельную тестовую PostgreSQL
базу через `TEST_DATABASE_URL`. Тестовая база не должна содержать рабочие данные:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/office_vault_test pytest -q
```

## Структура

См. раздел 23 ТЗ. Handlers вызывают сервисы, сервисы — репозитории.

## Безопасность

- Default Deny
- AES-256-GCM для секретов
- Проверка прав на каждый запрос
- Файлы только в MinIO (не Telegram file_id как единственное хранилище)
- Аудит всех действий
- `.env` исключён из Git и Docker build context; в образ не встраиваются credentials

## Лицензия

Проприетарная (внутренний корпоративный проект).
