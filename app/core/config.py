from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    bot_token: str
    superadmin_telegram_id: int

    # Database
    database_url: str

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # MinIO
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str
    minio_bucket: str = "office-vault"
    minio_secure: bool = False

    # Encryption
    master_encryption_key: str

    # App
    environment: str = "development"
    log_level: str = "INFO"

    # ClamAV (optional)
    clamav_host: str | None = None
    clamav_port: int = 3310


settings = Settings()
