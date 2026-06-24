from __future__ import annotations

from functools import lru_cache

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    app_env: str = Field(default="development", description="development | staging | production")
    app_secret_key: str = Field(..., description="Secret key for signing JWTs")
    debug: bool = Field(default=False)

    # ------------------------------------------------------------------
    # PostgreSQL
    # ------------------------------------------------------------------
    database_url: PostgresDsn = Field(
        ...,
        description="asyncpg DSN, e.g. postgresql+asyncpg://user:pass@host/db",
    )

    # ------------------------------------------------------------------
    # Redis (cache + Arq task queue)
    # ------------------------------------------------------------------
    redis_url: RedisDsn = Field(
        ...,
        description="Redis DSN, e.g. redis://localhost:6379/0",
    )

    # ------------------------------------------------------------------
    # S3-compatible object storage (Cloudflare R2 in prod, MinIO locally)
    # ------------------------------------------------------------------
    s3_endpoint_url: str = Field(..., description="e.g. http://minio:9000 or R2 endpoint")
    s3_access_key_id: str = Field(...)
    s3_secret_access_key: str = Field(...)
    s3_bucket_name: str = Field(default="weddingapp")
    s3_region: str = Field(default="auto")

    # ------------------------------------------------------------------
    # Razorpay (UPI / payment gateway)
    # ------------------------------------------------------------------
    razorpay_key_id: str = Field(...)
    razorpay_key_secret: str = Field(...)
    razorpay_webhook_secret: str = Field(...)

    # ------------------------------------------------------------------
    # Claude (Anthropic) — used for moderation and prompt generation
    # ------------------------------------------------------------------
    anthropic_api_key: str = Field(...)

    # ------------------------------------------------------------------
    # Generative media provider (swappable via adapters)
    # ------------------------------------------------------------------
    gen_provider: str = Field(
        default="stub",
        description="Which generation adapter to use: stub | stability | replicate | ...",
    )
    gen_provider_api_key: str = Field(default="")

    # ------------------------------------------------------------------
    # Worker settings
    # ------------------------------------------------------------------
    arq_max_jobs: int = Field(default=10)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
