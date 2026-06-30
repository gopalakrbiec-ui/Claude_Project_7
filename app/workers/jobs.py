from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation_job import JobStatus
from app.models.order import Order, OrderStatus
from app.repositories.generation_job import GenerationJobRepository
from app.repositories.order import OrderRepository
from app.services.agent import AgentService
from app.workers.pipeline import (
    run_build_prompt,
    run_generate,
    run_moderation,
    run_upload,
    run_watermark,
)

logger = logging.getLogger(__name__)

_TOOL_JOB_TTL = 86400  # 24 hours


# ---------------------------------------------------------------------------
# Tool job — async photo-tool execution
# ---------------------------------------------------------------------------


async def _dispatch_tool(ctx: dict, settings, tool_name: str, params: dict) -> bytes:
    """Run the appropriate adapter for tool_name and return result bytes."""
    storage = ctx.get("storage_adapter")

    def presign(key: str) -> str:
        from app.adapters.storage import S3StorageAdapter
        if isinstance(storage, S3StorageAdapter):
            return storage.presign(key, expires_in=3600)
        return f"fake://{key}"

    key_in = params.get("photo_key")
    fal_key = settings.gen_provider_api_key
    openai_key = settings.openai_api_key

    if tool_name == "face-swap":
        src_url = presign(params["source_key"])
        tgt_url = params.get("target_url_direct") or presign(params["target_key"])
        from app.adapters.face_swap import FalFaceSwapAdapter
        out = await FalFaceSwapAdapter(api_key=fal_key, cost_paise=params["cost_paise"]).swap(
            source_image_url=src_url, target_image_url=tgt_url
        )
        return out.media_bytes

    if tool_name == "restore":
        from app.adapters.photo_tools import PhotoRestoreAdapter
        data, _ = await PhotoRestoreAdapter(api_key=fal_key, cost_paise=params["cost_paise"]).restore(
            image_url=presign(key_in)
        )
        return data

    if tool_name == "bg-remove":
        from app.adapters.photo_tools import BgRemoveAdapter
        data, _ = await BgRemoveAdapter(api_key=fal_key, cost_paise=params["cost_paise"]).remove_bg(
            image_url=presign(key_in)
        )
        return data

    if tool_name == "upscale":
        from app.adapters.photo_tools import PhotoUpscaleAdapter
        data, _ = await PhotoUpscaleAdapter(api_key=fal_key, cost_paise=params["cost_paise"]).upscale(
            image_url=presign(key_in), scale=params.get("scale", 4)
        )
        return data

    if tool_name == "ai-filter":
        style = params.get("style", "anime")
        strength = params.get("strength", 0.75)
        if openai_key:
            import httpx
            from app.adapters.openai_image import OpenAIImageAdapter
            adapter = OpenAIImageAdapter(api_key=openai_key, cost_paise=params["cost_paise"])
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(presign(key_in))
                resp.raise_for_status()
                photo_bytes = resp.content
            data, _ = await adapter.style_filter(photo_bytes, style)
            return data
        from app.adapters.ai_tools import StyleTransferAdapter
        data, _ = await StyleTransferAdapter(api_key=fal_key, cost_paise=params["cost_paise"]).apply_style(
            image_url=presign(key_in), style=style, strength=strength
        )
        return data

    if tool_name == "ai-background":
        prompt = params.get("prompt", "")
        if openai_key:
            import httpx
            from app.adapters.openai_image import OpenAIImageAdapter
            adapter = OpenAIImageAdapter(api_key=openai_key, cost_paise=params["cost_paise"])
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(presign(key_in))
                resp.raise_for_status()
                photo_bytes = resp.content
            data, _ = await adapter.bg_replace(photo_bytes, prompt)
            return data
        from app.adapters.ai_tools import AiBgReplaceAdapter
        data, _ = await AiBgReplaceAdapter(api_key=fal_key, cost_paise=params["cost_paise"]).replace_bg(
            image_url=presign(key_in), prompt=prompt
        )
        return data

    if tool_name == "ai-outfit":
        person_url = presign(params["person_key"])
        garment_url = params.get("garment_image_url") or presign(params["garment_key"])
        from app.adapters.ai_tools import VirtualTryOnAdapter
        data, _ = await VirtualTryOnAdapter(api_key=fal_key, cost_paise=params["cost_paise"]).try_on(
            person_image_url=person_url,
            garment_image_url=garment_url,
            category=params.get("category", "upper_body"),
        )
        return data

    if tool_name == "hair-salon":
        from app.adapters.ai_tools import HairSalonAdapter
        data, _ = await HairSalonAdapter(api_key=fal_key, cost_paise=params["cost_paise"]).change_hair(
            image_url=presign(key_in),
            hair_style_image_url=params.get("hair_style_image_url"),
            hair_colour=params.get("hair_desc"),
        )
        return data

    if tool_name == "remix":
        from app.adapters.ai_tools import RemixAdapter
        data, _ = await RemixAdapter(api_key=fal_key, cost_paise=params["cost_paise"]).remix(
            person_image_url=presign(params["person_key"]),
            prompt=params.get("prompt", "creative remix"),
            style_image_url=params.get("style_image_url"),
            accessory_image_url=params.get("accessory_image_url"),
            strength=params.get("strength", 0.85),
        )
        return data

    if tool_name == "text-to-image":
        prompt = params.get("prompt", "")
        aspect_ratio = params.get("aspect_ratio", "9:16")
        if openai_key:
            from app.adapters.openai_image import OpenAIImageAdapter
            data, _ = await OpenAIImageAdapter(api_key=openai_key, cost_paise=params["cost_paise"]).generate(
                prompt, aspect_ratio=aspect_ratio
            )
            return data
        from app.adapters.ai_tools import TextToImageAdapter
        data, _ = await TextToImageAdapter(api_key=fal_key, cost_paise=params["cost_paise"]).generate(
            prompt=prompt, aspect_ratio=aspect_ratio
        )
        return data

    raise ValueError(f"Unknown tool: {tool_name}")


async def run_tool(
    ctx: dict,
    *,
    job_id: str,
    tool_name: str,
    user_id: int,
    cost_paise: int,
    params: dict,
) -> None:
    """
    Arq job: run an AI photo tool and publish result to Redis.

    Redis key: tool:job:{job_id}
    Value: JSON {status, result_url?, cost_paise, error?}
    TTL: 24 h
    """
    from app.core.redis import get_redis
    from app.core.config import get_settings

    redis = get_redis()
    redis_key = f"tool:job:{job_id}"

    await redis.set(
        redis_key,
        json.dumps({"status": "processing", "cost_paise": cost_paise}),
        ex=_TOOL_JOB_TTL,
    )

    try:
        settings = get_settings()
        storage = ctx.get("storage_adapter")

        result_bytes = await _dispatch_tool(ctx, settings, tool_name, params)

        # Apply watermark for all tools except bg-remove (transparent PNG)
        if tool_name != "bg-remove":
            import asyncio
            from app.workers.pipeline import _apply_watermark
            result_bytes = await asyncio.to_thread(_apply_watermark, result_bytes)

        # Upload result
        result_key = f"tool-results/{user_id}/{tool_name}/{uuid.uuid4()}.png"
        await storage.upload(result_key, result_bytes, content_type="image/png")

        # Build result URL
        from app.adapters.storage import S3StorageAdapter
        if isinstance(storage, S3StorageAdapter):
            result_url = storage.presign(result_key, expires_in=3600)
        else:
            result_url = f"fake://{result_key}"

        await redis.set(
            redis_key,
            json.dumps({"status": "done", "result_url": result_url, "cost_paise": cost_paise}),
            ex=_TOOL_JOB_TTL,
        )
        logger.info("run_tool: done job_id=%s tool=%s user=%s", job_id, tool_name, user_id)

    except Exception as exc:
        logger.exception("run_tool: failed job_id=%s tool=%s", job_id, tool_name)
        await redis.set(
            redis_key,
            json.dumps({"status": "failed", "error": str(exc)[:200], "cost_paise": cost_paise}),
            ex=_TOOL_JOB_TTL,
        )
        raise


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

        # ── Step 2: Resolve template metadata ────────────────────────────────
        from app.repositories.template import TemplateRepository
        template_repo = TemplateRepository(session)
        template = await template_repo.get_active(order.template_id)
        template_image_url: str | None = template.image_url if template else None
        template_scene: str | None = template.scene_description if template else None

        # Resolve user photo to a presigned URL the model can fetch
        face_image_url: str | None = None
        user_photo_key = order.input_payload.get("user_photo_key")
        if user_photo_key:
            storage = ctx.get("storage_adapter")
            from app.adapters.storage import S3StorageAdapter
            if isinstance(storage, S3StorageAdapter):
                face_image_url = storage.presign(user_photo_key, expires_in=900)

        aspect_ratio: str = order.input_payload.get("aspect_ratio", "9:16")

        # ── Step 3: Build prompt ──────────────────────────────────────────────
        prompt_result = await run_build_prompt(
            order,
            claude_adapter=ctx["claude_adapter"],
            template_scene=template_scene,
        )

        # ── Step 4: Generate ──────────────────────────────────────────────────
        media_type = order.input_payload.get("media_type", "image")
        provider_key = "video_provider" if media_type == "video" else "image_provider"
        generation_provider = ctx.get(provider_key) or ctx["generation_provider"]

        gen_output = await run_generate(
            order,
            job.id,
            prompt_result,
            session=session,
            generation_provider=generation_provider,
            template_image_url=template_image_url,
            face_image_url=face_image_url,
            aspect_ratio=aspect_ratio,
        )

        # ── Step 5: Watermark ─────────────────────────────────────────────────
        watermarked = await run_watermark(gen_output)

        # ── Step 6: Upload + commission — single atomic commit ────────────────
        #
        # commit=False defers the DB commit so we can include the commission
        # ledger row in the same transaction as order.status=done.
        # The S3 upload happens immediately (idempotent PUT); only the DB
        # commit is held.  If the worker crashes before the commit, the order
        # stays at status=generating and a retry re-runs from upload onward.
        await run_upload(
            order,
            job.id,
            watermarked,
            session=session,
            storage_adapter=ctx["storage_adapter"],
            media_type=gen_output.media_type,
            commit=False,
        )

        # Re-fetch order so agent_id and price_paise are loaded in this session
        await session.refresh(order)
        await AgentService(session).pay_commission(order)

        # Single commit: order=done + commission entry land together.
        await session.commit()
        logger.info("Order %s: upload + commission committed atomically", order.id)

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
