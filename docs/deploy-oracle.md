# Деплой на Oracle Cloud Always Free

Самый надёжный полностью бесплатный вариант для полного стека (Postgres + Redis + MinIO + бот).

## Шаги

1. Зарегистрироваться на https://www.oracle.com/cloud/free/ (нужна карта для верификации, но Always Free не списывает деньги).
2. Создать Always Free VM (Ampere A1 или AMD micro).
3. Установить Docker и Docker Compose:
   ```bash
   sudo dnf install -y docker docker-compose-plugin
   sudo systemctl enable --now docker
   sudo usermod -aG docker $USER
   ```
4. Клонировать репозиторий:
   ```bash
   git clone https://github.com/koba307/office-vault.git
   cd office-vault
   cp .env.example .env
   # Вписать BOT_TOKEN, SUPERADMIN_TELEGRAM_ID, пароли и MASTER_ENCRYPTION_KEY
   ```
5. Запустить:
   ```bash
   docker compose up -d --build
   docker compose logs -f bot
   ```

Бот будет работать 24/7 без засыпания.

Альтернативы (проще, но с ограничениями):
- Kerit Cloud / FPS.ms / Inject Cloud — бесплатные always-on для маленьких ботов (обычно 128–256 МБ, может не хватить на полный стек).
- Oracle — единственный вариант, где гарантированно влезет весь Docker Compose.
