from __future__ import annotations

import logging

from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.workers.jobs import generate_content

logger = logging.getLogger(__name__)


def _redis_settings() -> RedisSettings:
    settings = get_settings()
    url = str(settings.redis_url)
    # arq expects host/port/db separately; parse from DSN.
    # redis://host:port/db
    parts = url.replace("redis://", "").split("/")
    host_port = parts[0].split(":")
    host = host_port[0]
    port = int(host_port[1]) if len(host_port) > 1 else 6379
    db = int(parts[1]) if len(parts) > 1 else 0
    return RedisSettings(host=host, port=port, database=db)


async def on_startup(ctx: dict) -> None:
    """
    Initialise long-lived resources once per worker process and store them
    in the Arq context dict so job functions can access them.
    """
    from app.adapters.claude import ClaudePromptAdapter, FakeClaudeAdapter
    from app.adapters.generation import CompositeGenerationAdapter, FakeGenerationAdapter
    from app.adapters.moderation import ClaudeModerationAdapter, FakeModerationAdapter
    from app.adapters.storage import FakeStorageAdapter, S3StorageAdapter
    from app.core.database import get_engine

    settings = get_settings()

    # DB engine / session factory
    engine = get_engine()
    ctx["engine"] = engine

    # Adapters — swap to real implementations in production via settings.gen_provider
    if settings.app_env == "production":
        ctx["moderation_adapter"] = ClaudeModerationAdapter(api_key=settings.anthropic_api_key)
        ctx["claude_adapter"] = ClaudePromptAdapter(api_key=settings.anthropic_api_key)
        ctx["generation_provider"] = CompositeGenerationAdapter()
        ctx["storage_adapter"] = S3StorageAdapter(
            endpoint_url=settings.s3_endpoint_url,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            bucket_name=settings.s3_bucket_name,
            region=settings.s3_region,
        )
    else:
        logger.warning("Worker running in non-production mode — using fake adapters")
        ctx["moderation_adapter"] = FakeModerationAdapter()
        ctx["claude_adapter"] = FakeClaudeAdapter()
        ctx["generation_provider"] = FakeGenerationAdapter()
        ctx["storage_adapter"] = FakeStorageAdapter()

    logger.info("Worker startup complete (env=%s)", settings.app_env)


async def on_job_start(ctx: dict) -> None:
    """Open a fresh AsyncSession for each job."""
    from app.core.database import get_engine
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = ctx.get("engine") or get_engine()
    ctx["session"] = AsyncSession(engine, expire_on_commit=False)


async def on_job_end(ctx: dict) -> None:
    """Close the session after each job (success or failure)."""
    session: AsyncSession | None = ctx.pop("session", None)
    if session is not None:
        await session.close()


async def on_shutdown(ctx: dict) -> None:
    """Dispose the DB engine on worker shutdown."""
    engine = ctx.pop("engine", None)
    if engine is not None:
        await engine.dispose()
    logger.info("Worker shutdown complete")


class WorkerSettings:
    """Arq worker configuration."""

    redis_settings = _redis_settings()
    max_jobs = get_settings().arq_max_jobs
    functions = [generate_content]
    cron_jobs: list = []

    on_startup = on_startup
    on_shutdown = on_shutdown
    on_job_start = on_job_start
    on_job_end = on_job_end
