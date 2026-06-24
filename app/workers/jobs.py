from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation_job import JobStatus
from app.models.order import Order, OrderStatus
from app.repositories.generation_job import GenerationJobRepository
from app.repositories.order import OrderRepository
from app.workers.pipeline import (
    run_build_prompt,
    run_generate,
    run_moderation,
    run_upload,
    run_watermark,
)

logger = logging.getLogger(__name__)


async def generate_content(ctx: dict, *, order_id: int) -> None:
    """
    Arq job: run the full generation pipeline for an order.

    Retrieves adapter instances from the Arq worker context (populated by
    WorkerSettings.on_startup). Uses the same DB session for the whole job
    so that status transitions are visible atomically.

    The pipeline is:
      1. moderate  — safety gate; refunds and stops if blocked
      2. build_prompt — Claude turns customer input into a generation prompt
      3. generate  — image/video provider produces the output
      4. watermark — stamps preview outputs
      5. upload    — stores in R2/MinIO, marks order done

    Each step commits its own status transition so a crashed/retried worker
    can resume cleanly (terminal status check at the top prevents re-running
    already-done orders).
    """
    session: AsyncSession = ctx["session"]
    order_repo = OrderRepository(session)
    job_repo = GenerationJobRepository(session)

    order: Order | None = await order_repo.get_by_id(order_id)
    if order is None:
        logger.error("generate_content: order_id=%s not found — skipping", order_id)
        return

    # Guard: skip if already in a terminal state (worker retry after partial progress)
    if order.status in (OrderStatus.done, OrderStatus.rejected, OrderStatus.failed):
        logger.info(
            "generate_content: order_id=%s already in terminal status=%s — skipping",
            order_id,
            order.status,
        )
        return

    # Create or re-fetch the GenerationJob row (idempotent on retry)
    job = await job_repo.get_by_order_id(order_id)
    if job is None:
        job = await job_repo.create(order_id=order_id)
        await session.commit()

    try:
        # ── Step 1: Moderation ────────────────────────────────────────────────
        allowed = await run_moderation(
            order,
            job.id,
            session=session,
            moderation_adapter=ctx["moderation_adapter"],
        )
        if not allowed:
            return  # refunded + rejected inside run_moderation

        # Re-fetch order after commit so we have the latest status
        await session.refresh(order)

        # ── Step 2: Build prompt ──────────────────────────────────────────────
        prompt_result = await run_build_prompt(
            order,
            claude_adapter=ctx["claude_adapter"],
        )

        # ── Step 3: Generate ──────────────────────────────────────────────────
        # Select image or video provider based on the order's requested media type.
        # The order's input_payload carries "media_type": "image"|"video"; default image.
        media_type = order.input_payload.get("media_type", "image")
        provider_key = "video_provider" if media_type == "video" else "image_provider"
        # Fall back to legacy "generation_provider" key for backward compat
        generation_provider = ctx.get(provider_key) or ctx["generation_provider"]

        gen_output = await run_generate(
            order,
            job.id,
            prompt_result,
            session=session,
            generation_provider=generation_provider,
        )

        # ── Step 4: Watermark ─────────────────────────────────────────────────
        watermarked = await run_watermark(gen_output)

        # ── Step 5: Upload ────────────────────────────────────────────────────
        await run_upload(
            order,
            job.id,
            watermarked,
            session=session,
            storage_adapter=ctx["storage_adapter"],
            media_type=gen_output.media_type,
        )

    except Exception:
        logger.exception("generate_content: unhandled error for order_id=%s", order_id)
        await session.rollback()
        try:
            await job_repo.set_result(job.id, status=JobStatus.failed, error="Unhandled error")
            await order_repo.update_status(order_id, OrderStatus.failed)
            await session.commit()
        except Exception:
            logger.exception("generate_content: failed to mark job as failed for order_id=%s", order_id)
        raise  # re-raise so Arq can retry or log the failure
