from __future__ import annotations

"""
AI photo-tool endpoints — instant, synchronous transformations.

POST /tools/face-swap   — put the user's face into a template scene
POST /tools/restore     — deblur + enhance an old/low-quality photo
POST /tools/bg-remove   — remove photo background (transparent PNG)
POST /tools/upscale     — 4× Real-ESRGAN upscale

All endpoints:
  • require a logged-in user
  • deduct credits BEFORE calling the provider (same pattern as orders)
  • upload the result to R2 and return a short-lived presigned URL
  • apply the Yadein watermark to the output (except bg-remove)
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.ledger import LedgerReason
from app.models.user import User
from app.services.credits import CreditsService, InsufficientBalanceError, LedgerRef

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tools", tags=["tools"])


# ---------------------------------------------------------------------------
# Tool discovery — Flutter reads this to build the center AI button grid
# ---------------------------------------------------------------------------

_TOOL_CATALOG = [
    {"id": "face-swap",     "name": "Face Swap",        "icon": "face_retouching_natural", "cost_paise": 300,  "category": "portrait"},
    {"id": "ai-filter",     "name": "AI Filter",        "icon": "auto_awesome",            "cost_paise": 200,  "category": "style"},
    {"id": "bg-remove",     "name": "BG Remove",        "icon": "layers_clear",            "cost_paise": 150,  "category": "edit"},
    {"id": "ai-background", "name": "AI Background",    "icon": "landscape",               "cost_paise": 200,  "category": "edit"},
    {"id": "upscale",       "name": "Upscale HD",       "icon": "hd",                      "cost_paise": 150,  "category": "enhance"},
    {"id": "restore",       "name": "Photo Restore",    "icon": "restore",                 "cost_paise": 200,  "category": "enhance"},
    {"id": "ai-outfit",     "name": "AI Outfit",        "icon": "checkroom",               "cost_paise": 400,  "category": "fashion"},
    {"id": "hair-salon",    "name": "Hair Salon",        "icon": "content_cut",             "cost_paise": 200,  "category": "fashion"},
    {"id": "remix",         "name": "Remix",             "icon": "shuffle",                 "cost_paise": 500,  "category": "creative"},
    {"id": "text-to-image", "name": "Text to Image",    "icon": "text_fields",             "cost_paise": 200,  "category": "creative"},
]


@router.get("", summary="List all available AI tools")
async def list_tools() -> dict:
    """Returns the catalog of AI tools for the center button grid in Flutter."""
    return {"tools": _TOOL_CATALOG}

# Cost per tool in paise (can later be moved to config / DB)
_COST_FACE_SWAP = 300
_COST_RESTORE = 200
_COST_BG_REMOVE = 150
_COST_UPSCALE = 150
_COST_AI_FILTER = 200
_COST_TRYON = 400
_COST_HAIR = 200
_COST_BG_REPLACE = 200
_COST_REMIX = 500
_COST_TEXT2IMG = 200


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class FaceSwapIn(BaseModel):
    # source face — accept any field name Flutter might send
    source_photo_key: str | None = Field(default=None)
    face_photo_key: str | None = Field(default=None)
    # target body — accept any field name Flutter might send
    target_image_url: str | None = Field(default=None)
    target_photo_key: str | None = Field(default=None)
    target_body_key: str | None = Field(default=None)
    body_photo_key: str | None = Field(default=None)
    target_key: str | None = Field(default=None)

    @property
    def resolved_source_key(self) -> str | None:
        return self.source_photo_key or self.face_photo_key

    @property
    def resolved_target_key(self) -> str | None:
        return self.target_photo_key or self.target_body_key or self.body_photo_key or self.target_key


class ToolOut(BaseModel):
    result_url: str
    cost_paise: int


class RestoreIn(BaseModel):
    photo_key: str = Field(..., description="R2 key of the photo to restore (from /uploads/photo)")


class BgRemoveIn(BaseModel):
    photo_key: str = Field(..., description="R2 key of the photo (from /uploads/photo)")


class UpscaleIn(BaseModel):
    photo_key: str = Field(..., description="R2 key of the photo to upscale")
    scale: int = Field(default=4, ge=2, le=4)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _get_storage():
    from app.core.config import get_settings
    from app.adapters.storage import S3StorageAdapter, FakeStorageAdapter
    s = get_settings()
    if s.s3_endpoint_url and s.s3_access_key_id:
        return S3StorageAdapter(
            endpoint_url=str(s.s3_endpoint_url),
            access_key_id=s.s3_access_key_id,
            secret_access_key=s.s3_secret_access_key,
            bucket_name=s.s3_bucket_name,
            region=s.s3_region,
        )
    return FakeStorageAdapter()


def _presign(storage, key: str) -> str:
    from app.adapters.storage import S3StorageAdapter
    if isinstance(storage, S3StorageAdapter):
        return storage.presign(key, expires_in=3600)
    return f"fake://{key}"


async def _charge(
    db: AsyncSession,
    user: User,
    cost_paise: int,
    tool_name: str,
    idempotency_key: str,
) -> None:
    """Deduct credits. Raises 402 on insufficient balance."""
    from app.core.config import get_settings
    if get_settings().bypass_payments:
        logger.warning("bypass_payments: skipping credit charge for tool=%s user=%s", tool_name, user.id)
        return

    svc = CreditsService(db)
    try:
        await svc.debit(
            user_id=user.id,
            delta_paise=cost_paise,
            reason=LedgerReason.spend,
            ref=LedgerRef(ref_type="tool", ref_id=0),
            idempotency_key=idempotency_key,
        )
        await db.commit()
    except InsufficientBalanceError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "insufficient_balance",
                "available_paise": exc.available,
                "required_paise": exc.requested,
            },
        )


async def _upload_result(storage, user_id: int, tool_name: str, data: bytes, content_type: str) -> str:
    key = f"tool-results/{user_id}/{tool_name}/{uuid.uuid4()}.png"
    await storage.upload(key, data, content_type=content_type)
    return key


async def _download_bytes(url: str) -> bytes:
    import httpx
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


# ---------------------------------------------------------------------------
# Face Swap
# ---------------------------------------------------------------------------


@router.post("/face-swap", response_model=ToolOut)
async def face_swap(
    body: FaceSwapIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolOut:
    """Swap the user's face into a template or scene image."""
    from app.core.config import get_settings
    settings = get_settings()

    if not settings.gen_provider_api_key:
        raise HTTPException(status_code=503, detail="Face swap provider not configured")

    idem_key = f"tool:face-swap:{current_user.id}:{body.resolved_source_key}:{(body.target_image_url or body.resolved_target_key or '')[:64]}"
    await _charge(db, current_user, _COST_FACE_SWAP, "face-swap", idem_key)

    storage = _get_storage()
    src_key = body.resolved_source_key
    if not src_key:
        raise HTTPException(status_code=422, detail="Provide source_photo_key or face_photo_key")
    tgt_key = body.resolved_target_key
    if not body.target_image_url and not tgt_key:
        raise HTTPException(status_code=422, detail="Provide target_photo_key or target_body_key")
    source_url = _presign(storage, src_key)
    target_url = body.target_image_url or _presign(storage, tgt_key)

    from app.adapters.face_swap import FalFaceSwapAdapter
    adapter = FalFaceSwapAdapter(
        api_key=settings.gen_provider_api_key,
        cost_paise=_COST_FACE_SWAP,
    )
    try:
        output = await adapter.swap(
            source_image_url=source_url,
            target_image_url=target_url,
        )
    except Exception:
        logger.exception("face-swap failed for user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="Face swap failed — please try again")

    from app.workers.pipeline import _apply_watermark
    import asyncio
    watermarked = await asyncio.to_thread(_apply_watermark, output.media_bytes)

    key = await _upload_result(storage, current_user.id, "face-swap", watermarked, "image/png")
    return ToolOut(result_url=_presign(storage, key), cost_paise=_COST_FACE_SWAP)


# ---------------------------------------------------------------------------
# Photo Restore
# ---------------------------------------------------------------------------


@router.post("/restore", response_model=ToolOut)
async def restore_photo(
    body: RestoreIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolOut:
    """Restore and enhance an old or blurry photo."""
    from app.core.config import get_settings
    settings = get_settings()

    if not settings.gen_provider_api_key:
        raise HTTPException(status_code=503, detail="Restore provider not configured")

    idem_key = f"tool:restore:{current_user.id}:{body.photo_key}"
    await _charge(db, current_user, _COST_RESTORE, "restore", idem_key)

    storage = _get_storage()
    photo_url = _presign(storage, body.photo_key)

    from app.adapters.photo_tools import PhotoRestoreAdapter
    adapter = PhotoRestoreAdapter(api_key=settings.gen_provider_api_key, cost_paise=_COST_RESTORE)
    try:
        result_bytes, _ = await adapter.restore(image_url=photo_url)
    except Exception:
        logger.exception("restore failed for user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="Photo restore failed — please try again")

    import asyncio
    from app.workers.pipeline import _apply_watermark
    watermarked = await asyncio.to_thread(_apply_watermark, result_bytes)

    key = await _upload_result(storage, current_user.id, "restore", watermarked, "image/png")
    return ToolOut(result_url=_presign(storage, key), cost_paise=_COST_RESTORE)


# ---------------------------------------------------------------------------
# Background Remove
# ---------------------------------------------------------------------------


@router.post("/bg-remove", response_model=ToolOut)
async def bg_remove(
    body: BgRemoveIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolOut:
    """Remove background from a photo. Returns transparent PNG."""
    from app.core.config import get_settings
    settings = get_settings()

    if not settings.gen_provider_api_key:
        raise HTTPException(status_code=503, detail="BG remove provider not configured")

    idem_key = f"tool:bg-remove:{current_user.id}:{body.photo_key}"
    await _charge(db, current_user, _COST_BG_REMOVE, "bg-remove", idem_key)

    storage = _get_storage()
    photo_url = _presign(storage, body.photo_key)

    from app.adapters.photo_tools import BgRemoveAdapter
    adapter = BgRemoveAdapter(api_key=settings.gen_provider_api_key, cost_paise=_COST_BG_REMOVE)
    try:
        result_bytes, _ = await adapter.remove_bg(image_url=photo_url)
    except Exception:
        logger.exception("bg-remove failed for user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="Background removal failed — please try again")

    key = await _upload_result(storage, current_user.id, "bg-remove", result_bytes, "image/png")
    return ToolOut(result_url=_presign(storage, key), cost_paise=_COST_BG_REMOVE)


# ---------------------------------------------------------------------------
# Upscale
# ---------------------------------------------------------------------------


@router.post("/upscale", response_model=ToolOut)
async def upscale_photo(
    body: UpscaleIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolOut:
    """Upscale a photo up to 4× using Real-ESRGAN."""
    from app.core.config import get_settings
    settings = get_settings()

    if not settings.gen_provider_api_key:
        raise HTTPException(status_code=503, detail="Upscale provider not configured")

    idem_key = f"tool:upscale:{current_user.id}:{body.photo_key}:{body.scale}"
    await _charge(db, current_user, _COST_UPSCALE, "upscale", idem_key)

    storage = _get_storage()
    photo_url = _presign(storage, body.photo_key)

    from app.adapters.photo_tools import PhotoUpscaleAdapter
    adapter = PhotoUpscaleAdapter(api_key=settings.gen_provider_api_key, cost_paise=_COST_UPSCALE)
    try:
        result_bytes, _ = await adapter.upscale(image_url=photo_url, scale=body.scale)
    except Exception:
        logger.exception("upscale failed for user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="Upscale failed — please try again")

    key = await _upload_result(storage, current_user.id, "upscale", result_bytes, "image/png")
    return ToolOut(result_url=_presign(storage, key), cost_paise=_COST_UPSCALE)


# ---------------------------------------------------------------------------
# AI Filter — style transfer (anime, sketch, oil painting, etc.)
# ---------------------------------------------------------------------------


_STYLE_ALIASES: dict[str, str] = {
    "water": "watercolour", "watercolor": "watercolour", "watercolour": "watercolour",
    "anime": "anime", "cartoon": "anime",
    "sketch": "sketch", "pencil": "sketch", "drawing": "sketch",
    "oil": "oil_painting", "oil_painting": "oil_painting", "painting": "oil_painting",
    "cinematic": "cinematic", "movie": "cinematic", "film": "cinematic",
    "comic": "comic", "comics": "comic", "pop": "comic",
    "ghibli": "ghibli", "studio ghibli": "ghibli",
    "vintage": "vintage", "retro": "vintage", "old": "vintage",
    "bollywood": "bollywood",
    "royal": "royal",
}


class AiFilterIn(BaseModel):
    photo_key: str = Field(..., description="R2 key of the user's photo")
    style: str = Field(
        ...,
        description="Style: anime | sketch | oil_painting | cinematic | watercolour | comic | ghibli | vintage | bollywood | royal",
    )
    strength: float = Field(default=0.75, ge=0.1, le=1.0)


@router.post("/ai-filter", response_model=ToolOut)
async def ai_filter(
    body: AiFilterIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolOut:
    """Apply an artistic style filter to a photo (anime, sketch, oil painting, etc.)."""
    from app.core.config import get_settings
    settings = get_settings()
    if not settings.openai_api_key and not settings.gen_provider_api_key:
        raise HTTPException(status_code=503, detail="AI filter provider not configured")

    idem_key = f"tool:ai-filter:{current_user.id}:{body.photo_key}:{body.style}"
    await _charge(db, current_user, _COST_AI_FILTER, "ai-filter", idem_key)

    style = _STYLE_ALIASES.get(body.style.lower().strip(), body.style)

    storage = _get_storage()
    photo_url = _presign(storage, body.photo_key)
    photo_bytes = await _download_bytes(photo_url)

    try:
        if settings.openai_api_key:
            from app.adapters.openai_image import OpenAIImageAdapter
            adapter = OpenAIImageAdapter(api_key=settings.openai_api_key, cost_paise=_COST_AI_FILTER)
            result_bytes, _ = await adapter.style_filter(photo_bytes, style=style)
        else:
            from app.adapters.ai_tools import StyleTransferAdapter
            adapter = StyleTransferAdapter(api_key=settings.gen_provider_api_key, cost_paise=_COST_AI_FILTER)
            result_bytes, _ = await adapter.apply_style(image_url=photo_url, style=body.style, strength=body.strength)
    except Exception:
        logger.exception("ai-filter failed for user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="AI filter failed — please try again")

    import asyncio
    from app.workers.pipeline import _apply_watermark
    watermarked = await asyncio.to_thread(_apply_watermark, result_bytes)
    key = await _upload_result(storage, current_user.id, "ai-filter", watermarked, "image/png")
    return ToolOut(result_url=_presign(storage, key), cost_paise=_COST_AI_FILTER)


# ---------------------------------------------------------------------------
# AI Outfit / Virtual Try-On
# ---------------------------------------------------------------------------


class TryOnIn(BaseModel):
    person_photo_key: str = Field(..., description="R2 key of person's photo")
    garment_image_url: str | None = Field(default=None, description="Public URL of the garment/outfit image")
    garment_photo_key: str | None = Field(default=None, description="R2 key of garment photo (alternative to garment_image_url)")
    category: str = Field(default="upper_body", description="upper_body | lower_body | dresses")


@router.post("/ai-outfit", response_model=ToolOut)
async def ai_outfit(
    body: TryOnIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolOut:
    """Virtual try-on — dress the user in a garment image."""
    from app.core.config import get_settings
    settings = get_settings()
    if not settings.gen_provider_api_key:
        raise HTTPException(status_code=503, detail="Try-on provider not configured")

    idem_key = f"tool:ai-outfit:{current_user.id}:{body.person_photo_key}:{(body.garment_image_url or body.garment_photo_key or '')[:64]}"
    await _charge(db, current_user, _COST_TRYON, "ai-outfit", idem_key)

    if not body.garment_image_url and not body.garment_photo_key:
        raise HTTPException(status_code=422, detail="Provide garment_image_url or garment_photo_key")

    storage = _get_storage()
    person_url = _presign(storage, body.person_photo_key)
    garment_url = body.garment_image_url or _presign(storage, body.garment_photo_key)

    from app.adapters.ai_tools import VirtualTryOnAdapter
    adapter = VirtualTryOnAdapter(api_key=settings.gen_provider_api_key, cost_paise=_COST_TRYON)
    try:
        result_bytes, _ = await adapter.try_on(
            person_image_url=person_url,
            garment_image_url=garment_url,
            category=body.category,
        )
    except Exception:
        logger.exception("ai-outfit failed for user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="Virtual try-on failed — please try again")

    import asyncio
    from app.workers.pipeline import _apply_watermark
    watermarked = await asyncio.to_thread(_apply_watermark, result_bytes)
    key = await _upload_result(storage, current_user.id, "ai-outfit", watermarked, "image/png")
    return ToolOut(result_url=_presign(storage, key), cost_paise=_COST_TRYON)


# ---------------------------------------------------------------------------
# Hair Salon
# ---------------------------------------------------------------------------


class HairSalonIn(BaseModel):
    photo_key: str = Field(..., description="R2 key of user's photo")
    hair_colour: str | None = Field(
        default=None,
        description="Colour name: black | brown | blonde | red | auburn | grey | blue | pink | purple | green",
    )
    hair_style_image_url: str | None = Field(
        default=None, description="URL of a reference photo with the desired hair style"
    )


@router.post("/hair-salon", response_model=ToolOut)
async def hair_salon(
    body: HairSalonIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolOut:
    """Change hair colour or style. Provide colour name, style reference URL, or both."""
    if not body.hair_colour and not body.hair_style_image_url:
        raise HTTPException(status_code=422, detail="Provide at least hair_colour or hair_style_image_url")

    from app.core.config import get_settings
    settings = get_settings()
    if not settings.gen_provider_api_key:
        raise HTTPException(status_code=503, detail="Hair salon provider not configured")

    idem_key = f"tool:hair:{current_user.id}:{body.photo_key}:{body.hair_colour}:{body.hair_style_image_url}"
    await _charge(db, current_user, _COST_HAIR, "hair-salon", idem_key)

    storage = _get_storage()
    photo_url = _presign(storage, body.photo_key)

    from app.adapters.ai_tools import HairSalonAdapter
    adapter = HairSalonAdapter(api_key=settings.gen_provider_api_key, cost_paise=_COST_HAIR)
    try:
        result_bytes, _ = await adapter.change_hair(
            image_url=photo_url,
            hair_style_image_url=body.hair_style_image_url,
            hair_colour=body.hair_colour,
        )
    except Exception:
        logger.exception("hair-salon failed for user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="Hair salon failed — please try again")

    import asyncio
    from app.workers.pipeline import _apply_watermark
    watermarked = await asyncio.to_thread(_apply_watermark, result_bytes)
    key = await _upload_result(storage, current_user.id, "hair-salon", watermarked, "image/png")
    return ToolOut(result_url=_presign(storage, key), cost_paise=_COST_HAIR)


# ---------------------------------------------------------------------------
# AI Background Replace
# ---------------------------------------------------------------------------


class BgReplaceIn(BaseModel):
    photo_key: str = Field(..., description="R2 key of user's photo")
    prompt: str = Field(..., max_length=300, description="Description of the new background scene")


@router.post("/ai-background", response_model=ToolOut)
async def ai_background(
    body: BgReplaceIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolOut:
    """Replace photo background with an AI-generated scene."""
    from app.core.config import get_settings
    settings = get_settings()
    if not settings.openai_api_key and not settings.gen_provider_api_key:
        raise HTTPException(status_code=503, detail="AI background provider not configured")

    idem_key = f"tool:ai-bg:{current_user.id}:{body.photo_key}:{body.prompt[:64]}"
    await _charge(db, current_user, _COST_BG_REPLACE, "ai-background", idem_key)

    storage = _get_storage()
    photo_url = _presign(storage, body.photo_key)

    photo_bytes = await _download_bytes(photo_url)

    try:
        if settings.openai_api_key:
            from app.adapters.openai_image import OpenAIImageAdapter
            adapter = OpenAIImageAdapter(api_key=settings.openai_api_key, cost_paise=_COST_BG_REPLACE)
            result_bytes, _ = await adapter.bg_replace(photo_bytes, bg_prompt=body.prompt)
        else:
            from app.adapters.ai_tools import AiBgReplaceAdapter
            adapter = AiBgReplaceAdapter(api_key=settings.gen_provider_api_key, cost_paise=_COST_BG_REPLACE)
            result_bytes, _ = await adapter.replace_bg(image_url=photo_url, prompt=body.prompt)
    except Exception:
        logger.exception("ai-background failed for user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="AI background failed — please try again")

    import asyncio
    from app.workers.pipeline import _apply_watermark
    watermarked = await asyncio.to_thread(_apply_watermark, result_bytes)
    key = await _upload_result(storage, current_user.id, "ai-background", watermarked, "image/png")
    return ToolOut(result_url=_presign(storage, key), cost_paise=_COST_BG_REPLACE)


# ---------------------------------------------------------------------------
# Remix — multi-image fusion (person + outfit + accessory)
# ---------------------------------------------------------------------------


class RemixIn(BaseModel):
    person_photo_key: str = Field(..., description="R2 key of user's face/body photo")
    prompt: str = Field(..., max_length=500, description="Describe the desired output")
    style_image_url: str | None = Field(default=None, description="Outfit or style reference URL (Input2)")
    accessory_image_url: str | None = Field(default=None, description="Jewellery or prop reference URL (Input3)")
    strength: float = Field(default=0.85, ge=0.5, le=1.0)


@router.post("/remix", response_model=ToolOut)
async def remix(
    body: RemixIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolOut:
    """AI Mix / Remix — fuse person photo with outfit and accessory references."""
    from app.core.config import get_settings
    settings = get_settings()
    if not settings.gen_provider_api_key:
        raise HTTPException(status_code=503, detail="Remix provider not configured")

    idem_key = f"tool:remix:{current_user.id}:{body.person_photo_key}:{body.prompt[:64]}"
    await _charge(db, current_user, _COST_REMIX, "remix", idem_key)

    storage = _get_storage()
    person_url = _presign(storage, body.person_photo_key)

    from app.adapters.ai_tools import RemixAdapter
    adapter = RemixAdapter(api_key=settings.gen_provider_api_key, cost_paise=_COST_REMIX)
    try:
        result_bytes, _ = await adapter.remix(
            person_image_url=person_url,
            prompt=body.prompt,
            style_image_url=body.style_image_url,
            accessory_image_url=body.accessory_image_url,
            strength=body.strength,
        )
    except Exception:
        logger.exception("remix failed for user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="Remix failed — please try again")

    import asyncio
    from app.workers.pipeline import _apply_watermark
    watermarked = await asyncio.to_thread(_apply_watermark, result_bytes)
    key = await _upload_result(storage, current_user.id, "remix", watermarked, "image/png")
    return ToolOut(result_url=_presign(storage, key), cost_paise=_COST_REMIX)


# ---------------------------------------------------------------------------
# Text to Image — chat prompt → image
# ---------------------------------------------------------------------------


class TextToImageIn(BaseModel):
    prompt: str = Field(..., max_length=500, description="Describe the image you want to generate")
    aspect_ratio: str = Field(default="9:16", description="9:16 | 1:1 | 16:9 | 4:3 | 3:4")


@router.post("/text-to-image", response_model=ToolOut)
async def text_to_image(
    body: TextToImageIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolOut:
    """Generate an image from a text prompt (freeform chat-to-image)."""
    from app.core.config import get_settings
    settings = get_settings()
    if not settings.openai_api_key and not settings.gen_provider_api_key:
        raise HTTPException(status_code=503, detail="Text-to-image provider not configured")

    idem_key = f"tool:t2i:{current_user.id}:{body.prompt[:80]}:{body.aspect_ratio}"
    await _charge(db, current_user, _COST_TEXT2IMG, "text-to-image", idem_key)

    try:
        if settings.openai_api_key:
            from app.adapters.openai_image import OpenAIImageAdapter
            adapter = OpenAIImageAdapter(api_key=settings.openai_api_key, cost_paise=_COST_TEXT2IMG)
            result_bytes, _ = await adapter.generate(body.prompt, aspect_ratio=body.aspect_ratio)
        else:
            from app.adapters.ai_tools import TextToImageAdapter
            adapter = TextToImageAdapter(api_key=settings.gen_provider_api_key, cost_paise=_COST_TEXT2IMG)
            result_bytes, _ = await adapter.generate(prompt=body.prompt, aspect_ratio=body.aspect_ratio)
    except Exception:
        logger.exception("text-to-image failed for user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="Text to image failed — please try again")

    import asyncio
    from app.workers.pipeline import _apply_watermark
    watermarked = await asyncio.to_thread(_apply_watermark, result_bytes)
    storage = _get_storage()
    key = await _upload_result(storage, current_user.id, "text-to-image", watermarked, "image/png")
    return ToolOut(result_url=_presign(storage, key), cost_paise=_COST_TEXT2IMG)
