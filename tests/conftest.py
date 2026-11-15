import os

# Unit tests must not require production credentials from a developer's .env.
os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("SUPERADMIN_TELEGRAM_ID", "8666390663")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://officevault:test@127.0.0.1:5432/officevault_test"
)
os.environ.setdefault("MINIO_SECRET_KEY", "test-minio-secret")
os.environ.setdefault(
    "MASTER_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)
