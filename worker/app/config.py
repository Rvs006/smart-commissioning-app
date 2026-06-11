from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    environment: str = "development"
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql+psycopg://smart_commissioning:smart_commissioning@localhost:5432/smart_commissioning"
    object_storage_bucket: str = "commissioning-evidence"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> WorkerSettings:
    return WorkerSettings()

