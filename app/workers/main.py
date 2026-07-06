from __future__ import annotations

import logging

from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.workers.jobs import generate_content, run_tool

logger = logging.getLogger(__name__)


def _redis_settings() -> RedisSettings:
    from urllib.parse import urlparse
    settings = get_settings()
    parsed = urlparse(str(settings.redis_url))
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int(parsed.path.lstrip("/") or 0),
        password=parsed.password or None,
        ssl=parsed.scheme in ("rediss",),
    )


def _build_generation_providers(settings) -> tuple:
    """
    Return (image_provider, video_provider) based on GEN_PROVIDER config.

    GEN_PROVIDER=stub        → FakeGenerationAdapter (both paths)
    GEN_PROVIDER=composite   → CompositeGenerationAdapter (image), stub (video)
    GEN_PROVIDER=fal         → FalImageAdapter + FalVideoAdapter (real API calls)
    GEN_PROVIDER=instantid   → InstantIDAdapter (face-in-scene) + FalVideoAdapter
    GEN_PROVIDER=pollinations → PollinationsImageAdapter (free, no key)

    Swapping providers never touches business logic — only this function
    and .env need to change.
    """
    provider = settings.gen_provider.lower()

    if provider == "openai":
        from app.adapters.openai_image import OpenAIGenerationAdapter
        from app.adapters.generation import FakeVideoGenerationAdapter

        api_key = settings.openai_api_key or settings.gen_provider_api_key
        image_provider = OpenAIGenerationAdapter(
            api_key=api_key,
            cost_paise=settings.gen_image_cost_paise,
            timeout_seconds=settings.gen_image_timeout_seconds,
            model=settings.openai_image_model,
        )
        logger.info("Generation provider: openai/%s", settings.openai_image_model)
        return image_provider, FakeVideoGenerationAdapter()

    if provider == "fal":
        from app.adapters.fal import FalImageAdapter, FalVideoAdapter

        image_provider = FalImageAdapter(
            api_key=settings.gen_provider_api_key,
            model_id=settings.gen_image_model,
            cost_paise=settings.gen_image_cost_paise,
            max_retries=settings.gen_max_retries,
            timeout_seconds=settings.gen_image_timeout_seconds,
        )
        video_provider = FalVideoAdapter(
            api_key=settings.gen_provider_api_key,
            model_id=settings.gen_video_model,
            cost_paise=settings.gen_video_cost_paise,
            max_retries=settings.gen_max_retries,
            timeout_seconds=settings.gen_video_timeout_seconds,
        )
        logger.info(
            "Generation provider: fal.ai (image=%s, video=%s)",
            settings.gen_image_model,
            settings.gen_video_model,
        )
        return image_provider, video_provider

    if provider == "instantid":
        from app.adapters.instantid import InstantIDAdapter
        from app.adapters.fal import FalVideoAdapter
        from app.adapters.generation import FakeVideoGenerationAdapter

        image_provider = InstantIDAdapter(
            api_key=settings.gen_provider_api_key,
            cost_paise=settings.gen_image_cost_paise,
            timeout_seconds=settings.gen_image_timeout_seconds,
        )
        try:
            video_provider = FalVideoAdapter(
                api_key=settings.gen_provider_api_key,
                model_id=settings.gen_video_model,
                cost_paise=settings.gen_video_cost_paise,
                timeout_seconds=settings.gen_video_timeout_seconds,
            )
        except Exception:
            video_provider = FakeVideoGenerationAdapter()
        logger.info("Generation provider: fal.ai/instantid (face-in-scene)")
        return image_provider, video_provider

    if provider == "pollinations":
        from app.adapters.pollinations import PollinationsImageAdapter
        from app.adapters.generation import FakeVideoGenerationAdapter

        image_provider = PollinationsImageAdapter(
            cost_paise=settings.gen_image_cost_paise,
            timeout_seconds=settings.gen_image_timeout_seconds,
        )
        logger.info("Generation provider: pollinations.ai (free, no API key)")
        return image_provider, FakeVideoGenerationAdapter()

    if provider == "together":
        from app.adapters.together import TogetherImageAdapter
        from app.adapters.generation import FakeVideoGenerationAdapter

        api_key = settings.together_api_key or settings.gen_provider_api_key
        image_provider = TogetherImageAdapter(
            api_key=api_key,
            cost_paise=settings.gen_image_cost_paise,
            timeout_seconds=settings.gen_image_timeout_seconds,
        )
        logger.info("Generation provider: together.ai (FLUX.1-schnell-Free)")
        return image_provider, FakeVideoGenerationAdapter()

    if provider == "fireworks":
        from app.adapters.fireworks import FireworksImageAdapter
        from app.adapters.generation import FakeVideoGenerationAdapter

        api_key = settings.fireworks_api_key or settings.gen_provider_api_key
        image_provider = FireworksImageAdapter(
            api_key=api_key,
            cost_paise=settings.gen_image_cost_paise,
            timeout_seconds=settings.gen_image_timeout_seconds,
        )
        logger.info("Generation provider: fireworks.ai (flux-1-schnell-fp8)")
        return image_provider, FakeVideoGenerationAdapter()

    if provider == "composite":
        from app.adapters.generation import CompositeGenerationAdapter, FakeVideoGenerationAdapter

        logger.info("Generation provider: composite (image), fake (video)")
        return CompositeGenerationAdapter(), FakeVideoGenerationAdapter()

    # Default: stub — safe for local dev
    from app.adapters.generation import FakeGenerationAdapter, FakeVideoGenerationAdapter

    logger.warning(
        "Generation provider: stub (GEN_PROVIDER=%r) — not for production", settings.gen_provider
    )
    return FakeGenerationAdapter(), FakeVideoGenerationAdapter()


async def _reconcile_stuck_orders(ctx: dict) -> None:
    """
    On every worker start, find orders stuck in 'generating' or 'moderating'
    (left over from a crashed worker) and re-enqueue them.

    Safe to run multiple times — Arq deduplication and the terminal-status guard
    in generate_content prevent double-processing.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = ctx["engine"]
    arq_pool = ctx.get("arq_pool")

    async with AsyncSession(engine, expire_on_commit=False) as session:
        # Reset stuck generation_jobs rows
        await session.execute(text(
            "UPDATE generation_jobs SET status = 'pending', error = NULL "
            "WHERE order_id IN ("
            "  SELECT id FROM orders WHERE status IN ('generating', 'moderating')"
            ")"
        ))
        # Reset stuck orders to queued
        result = await session.execute(text(
            "UPDATE orders SET status = 'queued' "
            "WHERE status IN ('generating', 'moderating') "
            "RETURNING id"
        ))
        stuck_ids = [row[0] for row in result.fetchall()]
        await session.commit()

    if not stuck_ids:
        logger.info("Reconciliation: no stuck orders found")
        return

    logger.warning("Reconciliation: resetting %d stuck order(s): %s", len(stuck_ids), stuck_ids)

    # Re-enqueue each stuck order
    if arq_pool is None:
        # Build a temporary pool if not already in ctx
        import arq as arq_lib
        from urllib.parse import urlparse
        from arq.connections import RedisSettings as RS
        s = get_settings()
        parsed = urlparse(str(s.redis_url))
        arq_pool = await arq_lib.create_pool(RS(
            host=parsed.hostname or "localhost",
            port=parsed.port or 6379,
            database=int(parsed.path.lstrip("/") or 0),
            password=parsed.password or None,
            ssl=parsed.scheme in ("rediss",),
        ))
        close_after = True
    else:
        close_after = False

    for order_id in stuck_ids:
        await arq_pool.enqueue_job("generate_content", order_id=order_id)
        logger.info("Reconciliation: re-enqueued order_id=%s", order_id)

    if close_after:
        await arq_pool.aclose()


async def on_startup(ctx: dict) -> None:
    """
    Initialise long-lived resources once per worker process.
    All adapters are stored in ctx so job functions receive them via DI.
    """
    from app.adapters.claude import ClaudePromptAdapter, FakeClaudeAdapter
    from app.adapters.moderation import ClaudeModerationAdapter, FakeModerationAdapter
    from app.adapters.storage import FakeStorageAdapter, S3StorageAdapter
    from app.core.database import get_engine

    settings = get_settings()
    ctx["engine"] = get_engine()

    # Claude adapters — real in any env that has an API key configured
    use_claude = bool(settings.anthropic_api_key)
    if use_claude:
        ctx["moderation_adapter"] = ClaudeModerationAdapter(
            api_key=settings.anthropic_api_key,
            max_retries=settings.claude_max_retries,
            timeout_seconds=settings.claude_moderation_timeout_seconds,
        )
        ctx["claude_adapter"] = ClaudePromptAdapter(
            api_key=settings.anthropic_api_key,
            max_retries=settings.claude_max_retries,
            timeout_seconds=settings.claude_prompt_timeout_seconds,
        )
    else:
        logger.warning("ANTHROPIC_API_KEY not set — using fake moderation/prompt adapters")
        ctx["moderation_adapter"] = FakeModerationAdapter()
        ctx["claude_adapter"] = FakeClaudeAdapter()

    # Generation adapters — selected by GEN_PROVIDER setting
    image_provider, video_provider = _build_generation_providers(settings)
    ctx["image_provider"] = image_provider
    ctx["video_provider"] = video_provider
    # Backward compat: keep "generation_provider" pointing at image by default
    ctx["generation_provider"] = image_provider

    # Storage adapter
    use_real_storage = all([
        settings.s3_endpoint_url,
        settings.s3_access_key_id,
        settings.s3_secret_access_key,
    ])
    if use_real_storage:
        ctx["storage_adapter"] = S3StorageAdapter(
            endpoint_url=settings.s3_endpoint_url,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            bucket_name=settings.s3_bucket_name,
            region=settings.s3_region,
        )
    else:
        logger.warning("S3 not configured — using in-memory fake storage")
        ctx["storage_adapter"] = FakeStorageAdapter()

    logger.info(
        "Worker startup complete (env=%s, gen_provider=%s)",
        settings.app_env,
        settings.gen_provider,
    )

    # Reconcile stuck jobs from a previous crashed worker process.
    # Any order left in 'generating' or 'moderating' was interrupted mid-flight
    # and will never self-resolve. Reset them to 'queued' and re-enqueue.
    await _reconcile_stuck_orders(ctx)


async def on_job_start(ctx: dict) -> None:
    """Open a fresh AsyncSession for each job."""
    from app.core.database import get_engine

    engine = ctx.get("engine") or get_engine()
    ctx["session"] = AsyncSession(engine, expire_on_commit=False)


async def on_job_end(ctx: dict) -> None:
    """Close the session after each job (success or failure)."""
    session: AsyncSession | None = ctx.pop("session", None)
    if session is not None:
        await session.close()


async def on_shutdown(ctx: dict) -> None:
    """Dispose DB engine and close any adapter HTTP clients."""
    engine = ctx.pop("engine", None)
    if engine is not None:
        await engine.dispose()

    # Close fal.ai HTTP clients if present
    for key in ("image_provider", "video_provider"):
        adapter = ctx.pop(key, None)
        if adapter is not None and hasattr(adapter, "aclose"):
            await adapter.aclose()

    logger.info("Worker shutdown complete")


class WorkerSettings:
    """Arq worker configuration."""

    redis_settings = _redis_settings()
    max_jobs = get_settings().arq_max_jobs
    functions = [generate_content, run_tool]
    cron_jobs: list = []
    # arq's default job_timeout (300s) equals our OpenAI multi-image edit timeout,
    # so a job could be killed by arq right as the adapter itself would time out.
    # Give real headroom above the slowest adapter call (gen_video_timeout_seconds).
    job_timeout = 900

    on_startup = on_startup
    on_shutdown = on_shutdown
    on_job_start = on_job_start
    on_job_end = on_job_end
