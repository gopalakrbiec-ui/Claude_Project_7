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

_TOOL_JOB_TTL = 90 * 86400  # 90 days — matches per-user history sorted set TTL

# Each fal.ai video model accepts a different duration format/range. Flutter
# sends one shared duration value across all four video tools (e.g. Veo's
# "4s" format), which fails validation on Kling/Seedance/Wan. Normalize
# defensively instead of surfacing a raw fal.ai 422 to the user.
_VIDEO_DURATION_ALLOWED: dict[str, set[str]] = {
    "animate-photo":  {"5", "10"},
    "kling-video":    {"5", "10"},
    "seedance-video": {"2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"},
    "wan-video":      {"5"},
    "veo-video":      {"4s", "6s", "8s"},
}
_VIDEO_DURATION_DEFAULT: dict[str, str] = {
    "animate-photo":  "5",
    "kling-video":    "5",
    "seedance-video": "5",
    "wan-video":      "5",
    "veo-video":      "4s",
}


def _normalize_duration(tool_name: str, raw: str) -> str:
    """Coerce an incoming duration value to one this tool's fal.ai model accepts."""
    allowed = _VIDEO_DURATION_ALLOWED.get(tool_name, set())
    default = _VIDEO_DURATION_DEFAULT.get(tool_name, "5")
    if raw in allowed:
        return raw

    # Strip a trailing "s" and try to match/clamp to the nearest allowed value
    digits = raw[:-1] if raw.endswith("s") else raw
    try:
        n = int(digits)
    except ValueError:
        return default

    candidate = f"{n}s" if tool_name == "veo-video" else str(n)
    if candidate in allowed:
        return candidate

    # Clamp to nearest allowed numeric value
    numeric_allowed = sorted(
        int(v[:-1]) if v.endswith("s") else int(v) for v in allowed
    )
    if not numeric_allowed:
        return default
    nearest = min(numeric_allowed, key=lambda v: abs(v - n))
    return f"{nearest}s" if tool_name == "veo-video" else str(nearest)


def _user_facing_error(exc: Exception) -> str:
    """Translate known provider errors into a clean, user-facing message."""
    text = str(exc)
    if "safety system" in text or "rejected by the safety system" in text:
        return "Your photos couldn't be processed due to content safety policies. Please try different photos."
    if "Client error '400" in text and "images/edits" in text:
        return "This combination of photos couldn't be processed. Please try different photos."
    return text[:200]


# ---------------------------------------------------------------------------
# Tool job — async photo-tool execution
# ---------------------------------------------------------------------------


async def _fetch_bytes(url: str) -> bytes:
    import httpx
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


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
    openai_model = settings.openai_image_model
    cost = params["cost_paise"]

    # ── OpenAI gpt-image-2 tools (fal.ai fallback when key not set) ─────────

    if tool_name == "ai-filter":
        style = params.get("style", "anime")
        if openai_key:
            from app.adapters.openai_image import OpenAIImageAdapter
            photo_bytes = await _fetch_bytes(presign(key_in))
            data, _ = await OpenAIImageAdapter(api_key=openai_key, cost_paise=cost, model=openai_model, timeout_seconds=settings.gen_image_timeout_seconds).style_filter(photo_bytes, style)
            return data
        from app.adapters.ai_tools import StyleTransferAdapter
        data, _ = await StyleTransferAdapter(api_key=fal_key, cost_paise=cost).apply_style(
            image_url=presign(key_in), style=style, strength=params.get("strength", 0.75)
        )
        return data

    if tool_name == "ai-background":
        prompt = params.get("prompt", "")
        if openai_key:
            from app.adapters.openai_image import OpenAIImageAdapter
            photo_bytes = await _fetch_bytes(presign(key_in))
            data, _ = await OpenAIImageAdapter(api_key=openai_key, cost_paise=cost, model=openai_model, timeout_seconds=settings.gen_image_timeout_seconds).bg_replace(photo_bytes, prompt)
            return data
        from app.adapters.ai_tools import AiBgReplaceAdapter
        data, _ = await AiBgReplaceAdapter(api_key=fal_key, cost_paise=cost).replace_bg(
            image_url=presign(key_in), prompt=prompt
        )
        return data

    if tool_name == "ai-outfit":
        person_key = params["person_key"]
        garment_url = params.get("garment_image_url") or presign(params["garment_key"])
        if openai_key:
            from app.adapters.openai_image import OpenAIImageAdapter
            photo_bytes = await _fetch_bytes(presign(person_key))
            garment_bytes = await _fetch_bytes(garment_url)
            prompt = (
                "The first image is the person. The second image is the outfit/garment reference. "
                "Dress the person from the first image in the exact outfit shown in the second image. "
                "Keep the person's face, skin tone, body pose, and background unchanged. "
                "Only replace their clothing with the outfit from the second image."
            )
            data, _ = await OpenAIImageAdapter(api_key=openai_key, cost_paise=cost, model=openai_model, timeout_seconds=settings.gen_image_timeout_seconds).edit_multi(
                [photo_bytes, garment_bytes], prompt
            )
            return data
        person_url = presign(person_key)
        from app.adapters.ai_tools import VirtualTryOnAdapter
        data, _ = await VirtualTryOnAdapter(api_key=fal_key, cost_paise=cost).try_on(
            person_image_url=person_url,
            garment_image_url=garment_url,
            category=params.get("category", "upper_body"),
        )
        return data

    if tool_name == "hair-salon":
        hair_desc = params.get("hair_desc")
        if openai_key and hair_desc:
            from app.adapters.openai_image import OpenAIImageAdapter
            photo_bytes = await _fetch_bytes(presign(key_in))
            prompt = (
                f"Change the person's hair to: {hair_desc}. "
                "Keep the face, skin tone, clothing, pose, and background completely unchanged. "
                "Only the hair colour and style should change."
            )
            data, _ = await OpenAIImageAdapter(api_key=openai_key, cost_paise=cost, model=openai_model, timeout_seconds=settings.gen_image_timeout_seconds).edit(photo_bytes, prompt)
            return data
        from app.adapters.ai_tools import HairSalonAdapter
        data, _ = await HairSalonAdapter(api_key=fal_key, cost_paise=cost).change_hair(
            image_url=presign(key_in),
            hair_style_image_url=params.get("hair_style_image_url"),
            hair_colour=hair_desc,
        )
        return data

    if tool_name == "remix":
        prompt = params.get("prompt", "creative remix")
        if openai_key:
            from app.adapters.openai_image import OpenAIImageAdapter
            images = [await _fetch_bytes(presign(params["person_key"]))]
            ref_note = ""
            style_url = params.get("style_image_url")
            accessory_url = params.get("accessory_image_url")
            if style_url:
                images.append(await _fetch_bytes(style_url))
                ref_note += " The next image is a style reference — apply its visual style/theme."
            if accessory_url:
                images.append(await _fetch_bytes(accessory_url))
                ref_note += " The next image is an accessory/prop reference — incorporate it naturally."
            full_prompt = (
                f"The first image is the person. Creatively transform their photo: {prompt}.{ref_note} "
                "Keep the person's face and identity clearly recognisable."
            )
            data, _ = await OpenAIImageAdapter(api_key=openai_key, cost_paise=cost, model=openai_model, timeout_seconds=settings.gen_image_timeout_seconds).edit_multi(images, full_prompt)
            return data
        from app.adapters.ai_tools import RemixAdapter
        data, _ = await RemixAdapter(api_key=fal_key, cost_paise=cost).remix(
            person_image_url=presign(params["person_key"]),
            prompt=prompt,
            style_image_url=params.get("style_image_url"),
            accessory_image_url=params.get("accessory_image_url"),
            strength=params.get("strength", 0.85),
        )
        return data

    if tool_name == "photo-merge":
        photo_keys: list[str] = params["photo_keys"]
        scene: str = params.get("scene", "Place all the people together in one natural photo.")
        if not openai_key:
            raise ValueError("photo-merge requires OPENAI_API_KEY")
        from app.adapters.openai_image import OpenAIImageAdapter
        images: list[bytes] = []
        for pk in photo_keys:
            images.append(await _fetch_bytes(presign(pk)))
        full_prompt = (
            f"{scene} "
            "Keep the faces and identities of every person clearly recognisable. "
            "Preserve their skin tones, clothing colours, and distinctive features. "
            "Make the result look like a natural, high-quality photograph."
        )
        data, _ = await OpenAIImageAdapter(api_key=openai_key, cost_paise=cost, model=openai_model, timeout_seconds=settings.gen_image_timeout_seconds).edit_multi(
            images, full_prompt
        )
        return data

    if tool_name == "text-to-image":
        prompt = params.get("prompt", "")
        aspect_ratio = params.get("aspect_ratio", "9:16")
        if openai_key:
            from app.adapters.openai_image import OpenAIImageAdapter
            data, _ = await OpenAIImageAdapter(api_key=openai_key, cost_paise=cost, model=openai_model, timeout_seconds=settings.gen_image_timeout_seconds).generate(
                prompt, aspect_ratio=aspect_ratio
            )
            return data
        from app.adapters.ai_tools import TextToImageAdapter
        data, _ = await TextToImageAdapter(api_key=fal_key, cost_paise=cost).generate(
            prompt=prompt, aspect_ratio=aspect_ratio
        )
        return data

    # Image-to-video models (used when a photo is attached)
    _VIDEO_TOOL_MODELS: dict[str, str] = {
        "animate-photo":  settings.gen_video_model,  # backwards compat alias → Kling
        "kling-video":    settings.gen_video_model,  # fal-ai/kling-video/v2.1/standard/image-to-video
        "wan-video":      "fal-ai/wan-i2v",
        "seedance-video": "fal-ai/bytedance/seedance/v1/lite/image-to-video",
        "veo-video":      "fal-ai/veo3/fast/image-to-video",
    }
    # Text-to-video models (used when only a prompt is given)
    _VIDEO_TOOL_T2V_MODELS: dict[str, str] = {
        "animate-photo":  "fal-ai/kling-video/v2.1/standard/text-to-video",
        "kling-video":    "fal-ai/kling-video/v2.1/standard/text-to-video",
        "wan-video":      "fal-ai/wan-t2v",
        "seedance-video": "fal-ai/bytedance/seedance/v1/lite/text-to-video",
        "veo-video":      "fal-ai/veo3/fast",
    }

    if tool_name in _VIDEO_TOOL_MODELS:
        from app.adapters.fal import FalVideoAdapter
        prompt = params.get("prompt", "gentle motion, cinematic")
        duration = _normalize_duration(tool_name, params.get("duration", "5"))
        aspect_ratio = params.get("aspect_ratio", "9:16")

        if key_in:
            adapter = FalVideoAdapter(
                api_key=fal_key,
                model_id=_VIDEO_TOOL_MODELS[tool_name],
                cost_paise=cost,
                timeout_seconds=settings.gen_video_timeout_seconds,
            )
            output = await adapter.generate_from_image(
                prompt, image_url=presign(key_in), duration=duration, aspect_ratio=aspect_ratio
            )
        else:
            adapter = FalVideoAdapter(
                api_key=fal_key,
                model_id=_VIDEO_TOOL_T2V_MODELS[tool_name],
                cost_paise=cost,
                timeout_seconds=settings.gen_video_timeout_seconds,
            )
            output = await adapter.generate(prompt)
        return output.media_bytes

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

    import time as _time
    await redis.set(
        redis_key,
        json.dumps({"status": "processing", "cost_paise": cost_paise, "user_id": user_id, "tool_name": tool_name, "created_at": int(_time.time())}),
        ex=_TOOL_JOB_TTL,
    )

    try:
        settings = get_settings()
        storage = ctx.get("storage_adapter")

        result_bytes = await _dispatch_tool(ctx, settings, tool_name, params)

        # Video tools: skip watermark, use mp4 extension
        _VIDEO_TOOL_NAMES = {"animate-photo", "kling-video", "wan-video", "seedance-video", "veo-video"}
        is_video = tool_name in _VIDEO_TOOL_NAMES

        if not is_video and tool_name != "bg-remove":
            import asyncio
            from app.workers.pipeline import _apply_watermark
            result_bytes = await asyncio.to_thread(_apply_watermark, result_bytes)

        # Upload result
        ext = "mp4" if is_video else "png"
        content_type = "video/mp4" if is_video else "image/png"
        result_key = f"tool-results/{user_id}/{tool_name}/{uuid.uuid4()}.{ext}"
        await storage.upload(result_key, result_bytes, content_type=content_type)

        # Build result URL
        from app.adapters.storage import S3StorageAdapter
        if isinstance(storage, S3StorageAdapter):
            result_url = storage.presign(result_key, expires_in=3600)
        else:
            result_url = f"fake://{result_key}"

        await redis.set(
            redis_key,
            json.dumps({"status": "done", "result_url": result_url, "cost_paise": cost_paise, "user_id": user_id, "tool_name": tool_name}),
            ex=_TOOL_JOB_TTL,
        )
        logger.info("run_tool: done job_id=%s tool=%s user=%s", job_id, tool_name, user_id)

    except Exception as exc:
        logger.exception("run_tool: failed job_id=%s tool=%s", job_id, tool_name)

        # Refund the charge — the job failed before producing any output, so
        # the user should not be left out of pocket. Idempotent per job_id,
        # safe even if the job is retried. Skipped when bypass_payments is on
        # since no charge was ever made in that case.
        try:
            settings_for_refund = get_settings()
            if not settings_for_refund.bypass_payments and cost_paise > 0:
                from app.services.credits import CreditsService, LedgerRef
                from app.models.ledger import LedgerReason

                session = ctx.get("session")
                if session is not None:
                    await CreditsService(session).credit(
                        user_id=user_id,
                        delta_paise=cost_paise,
                        reason=LedgerReason.refund,
                        ref=LedgerRef(ref_type="tool", ref_id=0),
                        idempotency_key=f"refund:tool:{job_id}",
                    )
                    await session.commit()
                    logger.info("run_tool: refunded %d paise for failed job_id=%s user=%s", cost_paise, job_id, user_id)
        except Exception:
            logger.exception("run_tool: refund failed for job_id=%s user=%s — needs manual reconciliation", job_id, user_id)

        await redis.set(
            redis_key,
            json.dumps({"status": "failed", "error": _user_facing_error(exc), "cost_paise": cost_paise, "user_id": user_id, "tool_name": tool_name}),
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

        # Resolve user photo(s) to presigned URL(s) the model can fetch.
        # user_photo_keys (2-4 photos) takes priority over the single user_photo_key.
        face_image_url: str | None = None
        face_image_urls: list[str] | None = None
        storage = ctx.get("storage_adapter")
        from app.adapters.storage import S3StorageAdapter

        user_photo_keys = order.input_payload.get("user_photo_keys")
        if user_photo_keys and isinstance(storage, S3StorageAdapter):
            face_image_urls = [storage.presign(k, expires_in=900) for k in user_photo_keys]
            face_image_url = face_image_urls[0]
        else:
            user_photo_key = order.input_payload.get("user_photo_key")
            if user_photo_key and isinstance(storage, S3StorageAdapter):
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
            face_image_urls=face_image_urls,
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
