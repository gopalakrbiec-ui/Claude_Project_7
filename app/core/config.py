from __future__ import annotations

from functools import lru_cache

from pydantic import Field, PostgresDsn, RedisDsn, model_validator
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
    # Empty → worker falls back to FakeStorageAdapter (no uploads)
    # ------------------------------------------------------------------
    s3_endpoint_url: str = Field(default="", description="e.g. http://minio:9000 or R2 endpoint")
    s3_access_key_id: str = Field(default="")
    s3_secret_access_key: str = Field(default="")
    s3_bucket_name: str = Field(default="weddingapp")
    s3_region: str = Field(default="auto")

    # ------------------------------------------------------------------
    # Razorpay (UPI / payment gateway)
    # Empty → payment endpoints return errors but app starts
    # ------------------------------------------------------------------
    razorpay_key_id: str = Field(default="")
    razorpay_key_secret: str = Field(default="")
    razorpay_webhook_secret: str = Field(default="")

    # ------------------------------------------------------------------
    # Claude (Anthropic) — used for moderation and prompt generation
    # Empty → worker falls back to FakeModerationAdapter + FakeClaudeAdapter
    # ------------------------------------------------------------------
    anthropic_api_key: str = Field(default="")

    # ------------------------------------------------------------------
    # Generative media provider (swappable via adapters)
    # ------------------------------------------------------------------
    gen_provider: str = Field(
        default="stub",
        description="stub | fal | instantid | composite | pollinations | together | fireworks",
    )
    gen_provider_api_key: str = Field(default="")

    # OpenAI — gpt-image-1 for ai-filter, bg-replace, text-to-image tools
    openai_api_key: str = Field(default="")

    # Together AI (free Flux Schnell with API key)
    together_api_key: str = Field(default="")

    # Fireworks AI (~$0.002/image, fastest inference)
    fireworks_api_key: str = Field(default="")

    # fal.ai model selection (only used when gen_provider=fal)
    gen_image_model: str = Field(
        default="fal-ai/flux/dev",
        description="fal.ai model ID for image generation",
    )
    gen_video_model: str = Field(
        default="fal-ai/cogvideox-5b",
        description="fal.ai model ID for video generation",
    )

    # Cost (paise) charged to the ledger per output; set to match your fal.ai plan
    gen_image_cost_paise: int = Field(
        default=250,
        description="Standard cost per image in paise (₹2.50 default)",
    )
    gen_video_cost_paise: int = Field(
        default=800,
        description="Standard cost per video in paise (₹8.00 default)",
    )

    # Hard wall-clock budgets for the entire generate() call (submit + poll)
    gen_image_timeout_seconds: float = Field(default=300.0)
    gen_video_timeout_seconds: float = Field(default=600.0)
    gen_max_retries: int = Field(default=3)

    # ------------------------------------------------------------------
    # Claude adapter limits
    # ------------------------------------------------------------------
    claude_moderation_timeout_seconds: float = Field(default=30.0)
    claude_prompt_timeout_seconds: float = Field(default=60.0)
    claude_max_retries: int = Field(default=3)

    # ------------------------------------------------------------------
    # OTP / SMS delivery
    # otp_provider: console (dev) | msg91 (production)
    # ------------------------------------------------------------------
    # Set to true to skip credit balance checks — for testing template/generation
    # features before payments are wired. NEVER enable in production.
    bypass_payments: bool = Field(default=False)

    # Set to true to skip content moderation — for testing generation without
    # Anthropic API key. NEVER enable in production.
    bypass_moderation: bool = Field(default=False)

    # Set to true to accept any OTP code (use "000000") — for testing without SMS.
    # NEVER enable in production.
    bypass_otp: bool = Field(default=False)

    # Comma-separated list of allowed CORS origins in production.
    # e.g. "https://myapp.com,https://app.myapp.com"
    allowed_origins: str = Field(default="")

    otp_provider: str = Field(default="console", description="console | msg91 | twilio")
    msg91_auth_key: str = Field(default="")
    msg91_template_id: str = Field(default="")
    msg91_sender_id: str = Field(default="WDGAPP")
    twilio_account_sid: str = Field(default="")
    twilio_auth_token: str = Field(default="")
    twilio_from_number: str = Field(default="")

    # ------------------------------------------------------------------
    # Worker settings
    # ------------------------------------------------------------------
    arq_max_jobs: int = Field(default=10)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @model_validator(mode="after")
    def _forbid_dangerous_bypasses_in_production(self) -> "Settings":
        if self.app_env == "production":
            if self.bypass_otp:
                raise ValueError("bypass_otp must not be enabled in production")
            if self.bypass_payments:
                raise ValueError("bypass_payments must not be enabled in production")
            if self.bypass_moderation:
                raise ValueError("bypass_moderation must not be enabled in production")
            if not self.razorpay_webhook_secret:
                raise ValueError("RAZORPAY_WEBHOOK_SECRET is required in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
