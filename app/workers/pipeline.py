from __future__ import annotations

"""
Five standalone, unit-testable pipeline steps for the generation worker.

Idempotency on retry
---------------------
  run_moderation  — refund key "refund:order:{order_id}" is stable; second
                    credit call with same key is a no-op (returns existing entry).

  run_build_prompt — pure network call, no DB side-effects; safe to repeat.

  run_generate    — provider call may be re-invoked on retry; cost_paise is
                    overwritten with the same value. Future: attach a
                    provider-level idempotency key to prevent double billing.

  run_watermark   — pure bytes transform, always safe to repeat.

  run_upload      — S3 PUT is idempotent (same key → object overwritten).
                    output_keys written once on completion.
"""

import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.claude import ClaudeAdapter, PromptResult
from app.adapters.generation import (
    CostEvent,
    GenerationOutput,
    GenerationProvider,
    log_generation_cost,
)
from app.adapters.moderation import ModerationAdapter
from app.adapters.storage import StorageAdapter
from app.models.generation_job import JobStatus
from app.models.ledger import LedgerReason
from app.models.order import Order, OrderStatus
from app.repositories.generation_job import GenerationJobRepository
from app.repositories.order import OrderRepository
from app.services.credits import CreditsService, LedgerRef

logger = logging.getLogger(__name__)

_MEDIA_CONTENT_TYPES = {
    "image": "image/png",
    "video": "video/mp4",
}
_MEDIA_EXTENSIONS = {
    "image": "png",
    "video": "mp4",
}


async def run_moderation(
    order: Order,
    job_id: int,
    *,
    session: AsyncSession,
    moderation_adapter: ModerationAdapter,
) -> bool:
    """
    Run the safety gate. Returns True if allowed, False if blocked.

    On block: refunds credits (idempotent), sets order=rejected, job=rejected.
    """
    job_repo = GenerationJobRepository(session)
    order_repo = OrderRepository(session)
    credits_svc = CreditsService(session)

    await job_repo.update_status(job_id, JobStatus.moderating)
    await order_repo.update_status(order.id, OrderStatus.moderating)
    await session.commit()

    content = _payload_to_text(order.input_payload)
    result = await moderation_adapter.moderate(content)

    await job_repo.set_moderation_result(job_id, result.raw_response)

    if result.allowed:
        return True

    logger.info("Order %s blocked by moderation: %s", order.id, result.reason)

    # Refund is idempotent via its key — safe to replay if worker retries
    await credits_svc.credit(
        user_id=order.user_id,
        delta_paise=order.price_paise,
        reason=LedgerReason.refund,
        ref=LedgerRef(ref_type="order", ref_id=order.id),
        idempotency_key=f"refund:order:{order.id}",
    )

    await job_repo.set_result(job_id, status=JobStatus.rejected, error=result.reason)
    await order_repo.update_status(order.id, OrderStatus.rejected)
    await session.commit()

    return False


async def run_build_prompt(
    order: Order,
    *,
    claude_adapter: ClaudeAdapter,
) -> PromptResult:
    """
    Turn vernacular customer input into a clean generation prompt + caption copy.
    Pure network call — no DB writes.
    """
    language = order.input_payload.get("language", "hi")
    return await claude_adapter.build_prompt(order.input_payload, language)


async def run_generate(
    order: Order,
    job_id: int,
    prompt_result: PromptResult,
    *,
    session: AsyncSession,
    generation_provider: GenerationProvider,
) -> GenerationOutput:
    """
    Call the generation provider and record cost + provider on the job row.
    Fires the cost-logging hook after every successful call.
    """
    job_repo = GenerationJobRepository(session)
    order_repo = OrderRepository(session)

    await job_repo.update_status(job_id, JobStatus.generating)
    await order_repo.update_status(order.id, OrderStatus.generating)
    await session.commit()

    t0 = time.monotonic()
    output = await generation_provider.generate(prompt_result.generation_prompt)
    duration_ms = int((time.monotonic() - t0) * 1000)

    # Record provider name and cost (cost_paise is always integer, never float)
    await job_repo.set_result(
        job_id,
        status=JobStatus.generating,
        provider=output.provider_name,
        cost_paise=output.cost_paise,
    )
    await session.commit()

    # Cost-logging hook — fires after every generate() regardless of media type
    log_generation_cost(
        CostEvent(
            order_id=order.id,
            job_id=job_id,
            provider_name=output.provider_name,
            model_id=output.model_id,
            cost_paise=output.cost_paise,
            media_type=output.media_type,
            duration_ms=duration_ms,
        )
    )

    return output


async def run_watermark(output: GenerationOutput) -> bytes:
    """
    Stamp a preview watermark on generated output.
    Pure bytes transform — no DB writes, no external calls.
    Stub — real compositing wired in a future PR.
    """
    # TODO: overlay brand watermark using Pillow (image) or ffmpeg (video)
    return output.media_bytes


async def run_upload(
    order: Order,
    job_id: int,
    watermarked: bytes,
    *,
    session: AsyncSession,
    storage_adapter: StorageAdapter,
    media_type: str = "image",
) -> dict:
    """
    Upload the watermarked output to R2/MinIO and record the object keys.
    Content-type and file extension are derived from media_type.
    Returns the output_keys dict stored on the job row.
    """
    job_repo = GenerationJobRepository(session)
    order_repo = OrderRepository(session)

    ext = _MEDIA_EXTENSIONS.get(media_type, "bin")
    content_type = _MEDIA_CONTENT_TYPES.get(media_type, "application/octet-stream")
    key = f"orders/{order.id}/output.{ext}"

    await storage_adapter.upload(key, watermarked, content_type=content_type)

    output_keys = {media_type: key}

    await job_repo.set_result(job_id, status=JobStatus.done, output_keys=output_keys)
    await order_repo.update_status(order.id, OrderStatus.done)
    await session.commit()

    logger.info("Order %s done — uploaded to %s", order.id, key)
    return output_keys


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload_to_text(payload: dict) -> str:
    """Flatten the input payload to a plain string for the moderation prompt."""
    return "\n".join(f"{k}: {v}" for k, v in payload.items())
