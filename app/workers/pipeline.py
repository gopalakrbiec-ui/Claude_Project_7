from __future__ import annotations

"""
Five standalone, unit-testable pipeline steps for the generation worker.

Each function takes explicit dependencies (session, adapters) so the whole
pipeline can be exercised in tests without calling any real external API.

Idempotency on retry
---------------------
The Arq worker may retry a job if it crashes mid-flight. Each step is safe
to re-run:

  run_moderation  — GenerationJob.moderation_result is only written once;
                    the refund credit is idempotent via its key
                    "refund:order:{order_id}". If the worker crashes after
                    refunding but before setting status=rejected, the next
                    run will moderate again, get blocked again, and re-call
                    CreditsService.credit() with the same key → no-op.

  run_build_prompt — pure function of the order, idempotent by design.

  run_generate    — provider call may be re-invoked; cost_paise is
                    overwritten (same provider, same cost). In future, a
                    provider-level idempotency key can prevent double billing.

  run_watermark   — pure bytes transform, always safe to repeat.

  run_upload      — S3 PUT is idempotent (same key → same object overwritten).
                    output_keys written once on completion.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.claude import ClaudeAdapter, PromptResult
from app.adapters.generation import GenerationOutput, GenerationProvider
from app.adapters.moderation import ModerationAdapter
from app.adapters.storage import StorageAdapter
from app.models.generation_job import JobStatus
from app.models.ledger import LedgerReason
from app.models.order import Order, OrderStatus
from app.repositories.generation_job import GenerationJobRepository
from app.repositories.order import OrderRepository
from app.services.credits import CreditsService, LedgerRef

logger = logging.getLogger(__name__)


async def run_moderation(
    order: Order,
    job_id: int,
    *,
    session: AsyncSession,
    moderation_adapter: ModerationAdapter,
) -> bool:
    """
    Run the safety gate. Returns True if allowed, False if blocked.

    On block: refunds credits (idempotent), sets order status=rejected,
    sets job status=rejected with the moderation result stored for audit.
    """
    job_repo = GenerationJobRepository(session)
    order_repo = OrderRepository(session)
    credits_svc = CreditsService(session)

    # Set job + order to moderating
    await job_repo.update_status(job_id, JobStatus.moderating)
    await order_repo.update_status(order.id, OrderStatus.moderating)
    await session.commit()

    # Build the content string to moderate from the order's input payload
    content = _payload_to_text(order.input_payload)
    result = await moderation_adapter.moderate(content)

    # Persist the raw moderation response for audit / appeals
    await job_repo.set_moderation_result(job_id, result.raw_response)

    if result.allowed:
        return True

    # ── Blocked ──────────────────────────────────────────────────────────────
    logger.info(
        "Order %s blocked by moderation: %s", order.id, result.reason
    )

    # Refund the debit that was taken at order creation.
    # idempotency_key is stable — safe to replay if worker retries.
    await credits_svc.credit(
        user_id=order.user_id,
        delta_paise=order.price_paise,
        reason=LedgerReason.refund,
        ref=LedgerRef(ref_type="order", ref_id=order.id),
        idempotency_key=f"refund:order:{order.id}",
    )

    await job_repo.set_result(
        job_id,
        status=JobStatus.rejected,
        error=result.reason,
    )
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
    Call the image/video generation provider and record cost on the job row.
    """
    job_repo = GenerationJobRepository(session)
    order_repo = OrderRepository(session)

    await job_repo.update_status(job_id, JobStatus.generating)
    await order_repo.update_status(order.id, OrderStatus.generating)
    await session.commit()

    output = await generation_provider.generate(prompt_result.generation_prompt)

    # Record provider name and cost (cost_paise is always integer, never float)
    await job_repo.set_result(
        job_id,
        status=JobStatus.generating,
        provider=output.provider_name,
        cost_paise=output.cost_paise,
    )
    await session.commit()

    return output


async def run_watermark(output: GenerationOutput) -> bytes:
    """
    Stamp a preview watermark on generated output before delivery.
    Pure bytes transform — no DB writes, no external calls.
    Currently a stub; real compositing wired in a future PR.
    """
    # TODO: overlay brand watermark using Pillow or ffmpeg
    return output.image_bytes


async def run_upload(
    order: Order,
    job_id: int,
    watermarked: bytes,
    *,
    session: AsyncSession,
    storage_adapter: StorageAdapter,
) -> dict:
    """
    Upload the watermarked output to R2/MinIO and record the object keys.
    Returns the output_keys dict stored on the job row.
    """
    job_repo = GenerationJobRepository(session)
    order_repo = OrderRepository(session)

    key = f"orders/{order.id}/output.png"
    await storage_adapter.upload(key, watermarked, content_type="image/png")

    output_keys = {"image": key}

    await job_repo.set_result(
        job_id,
        status=JobStatus.done,
        output_keys=output_keys,
    )
    await order_repo.update_status(order.id, OrderStatus.done)
    await session.commit()

    logger.info("Order %s done — uploaded to %s", order.id, key)
    return output_keys


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload_to_text(payload: dict) -> str:
    """Flatten the input payload to a plain string for the moderation prompt."""
    parts: list[str] = []
    for k, v in payload.items():
        parts.append(f"{k}: {v}")
    return "\n".join(parts)
