"""
app/core/config.py
All configuration loaded from environment variables.
Never import settings at module level in tasks/models — use get_settings() to allow overrides in tests.
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────
    app_name: str = "txn-pipeline"
    app_env: Literal["development", "testing", "production"] = "development"
    log_level: str = "INFO"
    request_id_header: str = "X-Request-ID"

    # ── Database ─────────────────────────────────────────────────
    database_url: PostgresDsn = Field(
        ...,
        description="Async PostgreSQL DSN, e.g. postgresql+asyncpg://user:pass@host/db",
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 1800  # recycle connections every 30 min

    # ── Redis ────────────────────────────────────────────────────
    redis_url: RedisDsn = Field(..., description="redis://:password@host:6379/0")
    celery_broker_db: int = 0
    celery_result_db: int = 1   # separate DB from broker to avoid key collisions
    celery_result_expires: int = 86400  # 24h TTL on result tombstones

    # ── File Upload ───────────────────────────────────────────────
    upload_dir: Path = Path("/tmp/txn_uploads")
    max_upload_bytes: int = 10 * 1024 * 1024  # 10 MB hard limit
    allowed_mime_types: list[str] = ["text/csv", "application/csv", "text/plain"]

    # ── LLM ──────────────────────────────────────────────────────
    gemini_api_key: str = Field(..., description="Gemini API key — never hardcode")
    llm_model: str = "gemini-1.5-flash"
    llm_temperature: float = 0.0   # deterministic outputs
    llm_max_tokens: int = 2048
    llm_batch_size: int = 20       # max transactions per LLM call
    llm_retry_max: int = 3
    llm_retry_base_delay: float = 1.0  # seconds; doubles each attempt

    # ── Anomaly Detection ─────────────────────────────────────────
    anomaly_multiplier: float = 3.0
    domestic_only_merchants: list[str] = ["swiggy", "ola", "irctc", "zomato", "jio"]

    @field_validator("upload_dir", mode="before")
    @classmethod
    def ensure_upload_dir(cls, v: str | Path) -> Path:
        path = Path(v)
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
