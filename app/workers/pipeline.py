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

    from app.core.config import get_settings
    if get_settings().bypass_moderation:
        logger.warning("Moderation bypassed for order %s (bypass_moderation=true)", order.id)
        await job_repo.set_moderation_result(job_id, {"bypassed": True})
        return True

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
    template_scene: str | None = None,
) -> PromptResult:
    """
    Turn vernacular customer input into a clean generation prompt + caption copy.
    Merges template scene_description with user_prompt when available.
    Pure network call — no DB writes.
    """
    payload = dict(order.input_payload)

    # Merge new-style fields into payload for Claude
    user_prompt = payload.pop("user_prompt", None)
    if user_prompt:
        payload["user_prompt"] = user_prompt
    if template_scene:
        payload["scene_description"] = template_scene

    language = payload.get("language", "hi")
    return await claude_adapter.build_prompt(payload, language)


async def run_generate(
    order: Order,
    job_id: int,
    prompt_result: PromptResult,
    *,
    session: AsyncSession,
    generation_provider: GenerationProvider,
    template_image_url: str | None = None,
    face_image_url: str | None = None,
    aspect_ratio: str = "9:16",
) -> GenerationOutput:
    """
    Call the generation provider and record cost + provider on the job row.
    When face_image_url is present and the provider supports InstantID,
    generates a face-accurate portrait placed in the template scene.
    Fires the cost-logging hook after every successful call.
    """
    job_repo = GenerationJobRepository(session)
    order_repo = OrderRepository(session)

    await job_repo.update_status(job_id, JobStatus.generating)
    await order_repo.update_status(order.id, OrderStatus.generating)
    await session.commit()

    t0 = time.monotonic()

    # If provider supports face-in-scene and user uploaded a photo, use it
    from app.adapters.instantid import InstantIDAdapter
    if face_image_url and isinstance(generation_provider, InstantIDAdapter):
        output = await generation_provider.generate_with_face(
            prompt=prompt_result.generation_prompt,
            face_image_url=face_image_url,
            style_image_url=template_image_url,
            aspect_ratio=aspect_ratio,
        )
    else:
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
    Stamp the Yadein brand watermark on the bottom-centre of the image.
    Pure bytes transform — no DB writes, no external calls.
    Video output is returned unchanged (watermark via ffmpeg is a future task).
    """
    if output.media_type == "video":
        return output.media_bytes

    import asyncio
    return await asyncio.to_thread(_apply_watermark, output.media_bytes)


def _apply_watermark(image_bytes: bytes) -> bytes:
    """
    Overlay a semi-transparent 'Yadein ✨' pill at the bottom-centre.
    Uses Pillow — runs in a thread pool to avoid blocking the event loop.
    """
    try:
        import io
        from PIL import Image, ImageDraw, ImageFont

        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        w, h = img.size

        # Watermark text and sizing
        text = "✨ Yadein"
        font_size = max(24, h // 28)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except OSError:
            font = ImageFont.load_default()

        # Measure text
        dummy = Image.new("RGBA", (1, 1))
        draw_dummy = ImageDraw.Draw(dummy)
        bbox = draw_dummy.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

        # Draw pill background
        pad_x, pad_y = 20, 10
        pill_w, pill_h = tw + pad_x * 2, th + pad_y * 2
        pill_x = (w - pill_w) // 2
        pill_y = h - pill_h - max(20, h // 30)

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        draw.rounded_rectangle(
            [pill_x, pill_y, pill_x + pill_w, pill_y + pill_h],
            radius=pill_h // 2,
            fill=(0, 0, 0, 140),
        )
        draw.text((pill_x + pad_x, pill_y + pad_y), text, font=font, fill=(255, 255, 255, 230))

        composited = Image.alpha_composite(img, overlay).convert("RGB")
        out = io.BytesIO()
        composited.save(out, format="JPEG", quality=95)
        return out.getvalue()

    except Exception:
        logger.warning("Watermark failed — returning original bytes", exc_info=True)
        return image_bytes


async def run_upload(
    order: Order,
    job_id: int,
    watermarked: bytes,
    *,
    session: AsyncSession,
    storage_adapter: StorageAdapter,
    media_type: str = "image",
    commit: bool = True,
) -> dict:
    """
    Upload the watermarked output to R2/MinIO and record the object keys.
    Content-type and file extension are derived from media_type.
    Returns the output_keys dict stored on the job row.

    commit=False
    ------------
    Pass commit=False when the caller needs to add more writes to the same
    transaction before committing (e.g. a commission ledger entry).  The
    S3 upload is always performed immediately; only the DB commit is deferred.
    S3 PUT is idempotent so a crash before the DB commit is safe — a retry
    just re-uploads the same bytes to the same key.
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

    if commit:
        await session.commit()
        logger.info("Order %s done — uploaded to %s", order.id, key)

    return output_keys


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload_to_text(payload: dict) -> str:
    """Flatten the input payload to a plain string for the moderation prompt."""
    return "\n".join(f"{k}: {v}" for k, v in payload.items())
