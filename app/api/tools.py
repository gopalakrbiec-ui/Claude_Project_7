from __future__ import annotations

"""
AI photo-tool endpoints — async job pattern.

POST /tools/<name>      → charge credits, enqueue arq job, return {job_id, status, cost_paise}
GET  /tools/status/{id} → poll job status from Redis; returns result_url when done

All POST endpoints:
  • require a logged-in user
  • deduct credits BEFORE enqueuing (same pattern as orders)
  • return immediately so Railway's 60 s timeout is never hit
"""

import json
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


# Cost per tool in paise
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
    model_config = {"extra": "allow"}

    source_photo_key: str | None = Field(default=None)
    face_photo_key: str | None = Field(default=None)
    photo_key: str | None = Field(default=None)
    target_image_url: str | None = Field(default=None)
    target_photo_key: str | None = Field(default=None)
    target_body_key: str | None = Field(default=None)
    body_photo_key: str | None = Field(default=None)
    target_key: str | None = Field(default=None)

    @property
    def resolved_source_key(self) -> str | None:
        known = self.source_photo_key or self.face_photo_key or self.photo_key
        if known:
            return known
        extras = {k: v for k, v in (self.model_extra or {}).items() if v and isinstance(v, str)}
        for k, v in extras.items():
            if "source" in k or "face" in k:
                return v
        return next(iter(extras.values()), None)

    @property
    def resolved_target_key(self) -> str | None:
        known = self.target_photo_key or self.target_body_key or self.body_photo_key or self.target_key
        if known:
            return known
        extras = {k: v for k, v in (self.model_extra or {}).items() if v and isinstance(v, str)}
        for k, v in extras.items():
            if "target" in k or "body" in k:
                return v
        values = list(extras.values())
        return values[1] if len(values) > 1 else None


class JobOut(BaseModel):
    """Returned immediately when a tool job is enqueued."""
    job_id: str
    status: str = "processing"
    cost_paise: int


class JobStatusOut(BaseModel):
    """Returned by GET /tools/status/{job_id}."""
    job_id: str
    status: str          # processing | done | failed
    cost_paise: int | None = None
    result_url: str | None = None
    error: str | None = None


class RestoreIn(BaseModel):
    photo_key: str = Field(..., description="R2 key of the photo to restore")


class BgRemoveIn(BaseModel):
    photo_key: str = Field(..., description="R2 key of the photo")


class UpscaleIn(BaseModel):
    photo_key: str = Field(..., description="R2 key of the photo to upscale")
    scale: int = Field(default=4, ge=2, le=4)


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
    style: str = Field(..., description="Style: anime | sketch | oil_painting | cinematic | watercolour | comic | ghibli | vintage | bollywood | royal")
    strength: float = Field(default=0.75, ge=0.1, le=1.0)


class TryOnIn(BaseModel):
    model_config = {"extra": "allow"}

    person_photo_key: str | None = Field(default=None)
    photo_key: str | None = Field(default=None)
    user_photo_key: str | None = Field(default=None)
    garment_image_url: str | None = Field(default=None)
    garment_photo_key: str | None = Field(default=None)
    outfit_photo_key: str | None = Field(default=None)
    clothing_photo_key: str | None = Field(default=None)
    category: str = Field(default="upper_body")

    @property
    def resolved_person_key(self) -> str | None:
        known = self.person_photo_key or self.photo_key or self.user_photo_key
        if known:
            return known
        extras = {k: v for k, v in (self.model_extra or {}).items() if v and isinstance(v, str)}
        for k, v in extras.items():
            if "person" in k or "user" in k or "photo" in k or "your" in k:
                return v
        return next(iter(extras.values()), None)

    @property
    def resolved_garment_key(self) -> str | None:
        known = self.garment_photo_key or self.outfit_photo_key or self.clothing_photo_key
        if known:
            return known
        extras = {k: v for k, v in (self.model_extra or {}).items() if v and isinstance(v, str)}
        for k, v in extras.items():
            if "garment" in k or "outfit" in k or "cloth" in k:
                return v
        values = list(extras.values())
        return values[1] if len(values) > 1 else None


class HairSalonIn(BaseModel):
    photo_key: str = Field(..., description="R2 key of user's photo")
    hair_colour: str | None = Field(default=None)
    hair_color: str | None = Field(default=None)
    hair_style: str | None = Field(default=None)
    hair_style_image_url: str | None = Field(default=None)

    @property
    def resolved_hair_desc(self) -> str | None:
        return self.hair_colour or self.hair_color or self.hair_style


class BgReplaceIn(BaseModel):
    photo_key: str = Field(..., description="R2 key of user's photo")
    prompt: str = Field(..., max_length=300, description="Description of the new background scene")


class RemixIn(BaseModel):
    model_config = {"extra": "allow"}

    person_photo_key: str | None = Field(default=None)
    photo_key: str | None = Field(default=None)
    prompt: str | None = Field(default=None, max_length=500)
    style: str | None = Field(default=None, max_length=500)
    remix_style: str | None = Field(default=None, max_length=500)
    style_image_url: str | None = Field(default=None)
    accessory_image_url: str | None = Field(default=None)
    strength: float = Field(default=0.85, ge=0.5, le=1.0)

    @property
    def resolved_person_key(self) -> str | None:
        known = self.person_photo_key or self.photo_key
        if known:
            return known
        extras = {k: v for k, v in (self.model_extra or {}).items() if v and isinstance(v, str)}
        for k, v in extras.items():
            if "person" in k or "photo" in k or "user" in k:
                return v
        return next(iter(extras.values()), None)

    @property
    def resolved_prompt(self) -> str:
        return self.prompt or self.style or self.remix_style or "creative remix"


class TextToImageIn(BaseModel):
    prompt: str = Field(..., max_length=500, description="Describe the image you want to generate")
    aspect_ratio: str = Field(default="9:16", description="9:16 | 1:1 | 16:9 | 4:3 | 3:4")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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


async def _enqueue(tool_name: str, user_id: int, cost_paise: int, params: dict) -> str:
    """Enqueue an arq run_tool job and return the job_id."""
    import arq
    from app.core.config import get_settings
    from urllib.parse import urlparse
    from arq.connections import RedisSettings

    job_id = str(uuid.uuid4())
    settings = get_settings()
    parsed = urlparse(str(settings.redis_url))
    pool = await arq.create_pool(RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int(parsed.path.lstrip("/") or 0),
        password=parsed.password or None,
        ssl=parsed.scheme in ("rediss",),
    ))
    try:
        await pool.enqueue_job(
            "run_tool",
            job_id=job_id,
            tool_name=tool_name,
            user_id=user_id,
            cost_paise=cost_paise,
            params=params,
            _job_id=f"tool:{job_id}",
        )
    finally:
        await pool.aclose()

    return job_id


# ---------------------------------------------------------------------------
# Status polling
# ---------------------------------------------------------------------------


@router.get("/status/{job_id}", response_model=JobStatusOut)
async def tool_status(
    job_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
) -> JobStatusOut:
    """Poll the status of an async tool job."""
    from app.core.redis import get_redis

    redis = get_redis()
    raw = await redis.get(f"tool:job:{job_id}")
    if raw is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    data = json.loads(raw)
    return JobStatusOut(
        job_id=job_id,
        status=data.get("status", "processing"),
        cost_paise=data.get("cost_paise"),
        result_url=data.get("result_url"),
        error=data.get("error"),
    )


# ---------------------------------------------------------------------------
# Face Swap
# ---------------------------------------------------------------------------


@router.post("/face-swap", response_model=JobOut)
async def face_swap(
    body: FaceSwapIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    """Swap the user's face into a template or scene image."""
    logger.warning("face-swap body fields: %s", {k: v for k, v in body.model_dump().items() if v is not None})
    if body.model_extra:
        logger.warning("face-swap EXTRA fields: %s", body.model_extra)

    src_key = body.resolved_source_key
    if not src_key:
        raise HTTPException(status_code=422, detail="Provide source_photo_key or face_photo_key")
    tgt_key = body.resolved_target_key
    if not body.target_image_url and not tgt_key:
        raise HTTPException(status_code=422, detail="Provide target_photo_key or target_body_key")

    idem_key = f"tool:face-swap:{current_user.id}:{src_key}:{(body.target_image_url or tgt_key or '')[:64]}"
    await _charge(db, current_user, _COST_FACE_SWAP, "face-swap", idem_key)

    # If target_image_url is an external URL, pass it directly; otherwise pass the R2 key
    params: dict = {"source_key": src_key, "cost_paise": _COST_FACE_SWAP}
    if body.target_image_url:
        params["target_url_direct"] = body.target_image_url
        params["target_key"] = src_key  # placeholder; worker uses target_url_direct
    else:
        params["target_key"] = tgt_key

    job_id = await _enqueue("face-swap", current_user.id, _COST_FACE_SWAP, params)
    return JobOut(job_id=job_id, cost_paise=_COST_FACE_SWAP)


# ---------------------------------------------------------------------------
# Photo Restore
# ---------------------------------------------------------------------------


@router.post("/restore", response_model=JobOut)
async def restore_photo(
    body: RestoreIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    """Restore and enhance an old or blurry photo."""
    idem_key = f"tool:restore:{current_user.id}:{body.photo_key}"
    await _charge(db, current_user, _COST_RESTORE, "restore", idem_key)
    job_id = await _enqueue("restore", current_user.id, _COST_RESTORE, {"photo_key": body.photo_key, "cost_paise": _COST_RESTORE})
    return JobOut(job_id=job_id, cost_paise=_COST_RESTORE)


# ---------------------------------------------------------------------------
# Background Remove
# ---------------------------------------------------------------------------


@router.post("/bg-remove", response_model=JobOut)
async def bg_remove(
    body: BgRemoveIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    """Remove background from a photo. Returns transparent PNG."""
    idem_key = f"tool:bg-remove:{current_user.id}:{body.photo_key}"
    await _charge(db, current_user, _COST_BG_REMOVE, "bg-remove", idem_key)
    job_id = await _enqueue("bg-remove", current_user.id, _COST_BG_REMOVE, {"photo_key": body.photo_key, "cost_paise": _COST_BG_REMOVE})
    return JobOut(job_id=job_id, cost_paise=_COST_BG_REMOVE)


# ---------------------------------------------------------------------------
# Upscale
# ---------------------------------------------------------------------------


@router.post("/upscale", response_model=JobOut)
async def upscale_photo(
    body: UpscaleIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    """Upscale a photo up to 4× using Real-ESRGAN."""
    idem_key = f"tool:upscale:{current_user.id}:{body.photo_key}:{body.scale}"
    await _charge(db, current_user, _COST_UPSCALE, "upscale", idem_key)
    job_id = await _enqueue("upscale", current_user.id, _COST_UPSCALE, {"photo_key": body.photo_key, "scale": body.scale, "cost_paise": _COST_UPSCALE})
    return JobOut(job_id=job_id, cost_paise=_COST_UPSCALE)


# ---------------------------------------------------------------------------
# AI Filter
# ---------------------------------------------------------------------------


@router.post("/ai-filter", response_model=JobOut)
async def ai_filter(
    body: AiFilterIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    """Apply an artistic style filter to a photo."""
    idem_key = f"tool:ai-filter:{current_user.id}:{body.photo_key}:{body.style}"
    await _charge(db, current_user, _COST_AI_FILTER, "ai-filter", idem_key)
    style = _STYLE_ALIASES.get(body.style.lower().strip(), body.style)
    job_id = await _enqueue("ai-filter", current_user.id, _COST_AI_FILTER, {
        "photo_key": body.photo_key, "style": style, "strength": body.strength, "cost_paise": _COST_AI_FILTER,
    })
    return JobOut(job_id=job_id, cost_paise=_COST_AI_FILTER)


# ---------------------------------------------------------------------------
# AI Outfit / Virtual Try-On
# ---------------------------------------------------------------------------


@router.post("/ai-outfit", response_model=JobOut)
async def ai_outfit(
    body: TryOnIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    """Virtual try-on — dress the user in a garment image."""
    logger.warning("ai-outfit body: %s extras: %s",
        {k: v for k, v in body.model_dump().items() if v and k != "model_config"},
        body.model_extra)

    person_key = body.resolved_person_key
    if not person_key:
        raise HTTPException(status_code=422, detail="Provide person_photo_key")
    garment_key = body.resolved_garment_key
    if not body.garment_image_url and not garment_key:
        raise HTTPException(status_code=422, detail="Provide garment_photo_key or outfit_photo_key")

    idem_key = f"tool:ai-outfit:{current_user.id}:{person_key}:{(body.garment_image_url or garment_key or '')[:64]}"
    await _charge(db, current_user, _COST_TRYON, "ai-outfit", idem_key)

    job_id = await _enqueue("ai-outfit", current_user.id, _COST_TRYON, {
        "person_key": person_key,
        "garment_key": garment_key or person_key,  # fallback; garment_image_url handled in worker
        "garment_image_url": body.garment_image_url,
        "category": body.category,
        "cost_paise": _COST_TRYON,
    })
    return JobOut(job_id=job_id, cost_paise=_COST_TRYON)


# ---------------------------------------------------------------------------
# Hair Salon
# ---------------------------------------------------------------------------


@router.post("/hair-salon", response_model=JobOut)
async def hair_salon(
    body: HairSalonIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    """Change hair colour or style."""
    if not body.resolved_hair_desc and not body.hair_style_image_url:
        raise HTTPException(status_code=422, detail="Provide at least hair_colour or hair_style_image_url")

    idem_key = f"tool:hair:{current_user.id}:{body.photo_key}:{body.hair_colour}:{body.hair_style_image_url}"
    await _charge(db, current_user, _COST_HAIR, "hair-salon", idem_key)

    job_id = await _enqueue("hair-salon", current_user.id, _COST_HAIR, {
        "photo_key": body.photo_key,
        "hair_desc": body.resolved_hair_desc,
        "hair_style_image_url": body.hair_style_image_url,
        "cost_paise": _COST_HAIR,
    })
    return JobOut(job_id=job_id, cost_paise=_COST_HAIR)


# ---------------------------------------------------------------------------
# AI Background Replace
# ---------------------------------------------------------------------------


@router.post("/ai-background", response_model=JobOut)
async def ai_background(
    body: BgReplaceIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    """Replace photo background with an AI-generated scene."""
    idem_key = f"tool:ai-bg:{current_user.id}:{body.photo_key}:{body.prompt[:64]}"
    await _charge(db, current_user, _COST_BG_REPLACE, "ai-background", idem_key)

    job_id = await _enqueue("ai-background", current_user.id, _COST_BG_REPLACE, {
        "photo_key": body.photo_key, "prompt": body.prompt, "cost_paise": _COST_BG_REPLACE,
    })
    return JobOut(job_id=job_id, cost_paise=_COST_BG_REPLACE)


# ---------------------------------------------------------------------------
# Remix
# ---------------------------------------------------------------------------


@router.post("/remix", response_model=JobOut)
async def remix(
    body: RemixIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    """AI Mix / Remix — fuse person photo with outfit and accessory references."""
    person_key = body.resolved_person_key
    if not person_key:
        raise HTTPException(status_code=422, detail="Provide person_photo_key or photo_key")

    idem_key = f"tool:remix:{current_user.id}:{person_key}:{body.resolved_prompt[:64]}"
    await _charge(db, current_user, _COST_REMIX, "remix", idem_key)

    job_id = await _enqueue("remix", current_user.id, _COST_REMIX, {
        "person_key": person_key,
        "prompt": body.resolved_prompt,
        "style_image_url": body.style_image_url,
        "accessory_image_url": body.accessory_image_url,
        "strength": body.strength,
        "cost_paise": _COST_REMIX,
    })
    return JobOut(job_id=job_id, cost_paise=_COST_REMIX)


# ---------------------------------------------------------------------------
# Text to Image
# ---------------------------------------------------------------------------


@router.post("/text-to-image", response_model=JobOut)
async def text_to_image(
    body: TextToImageIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    """Generate an image from a text prompt (freeform chat-to-image)."""
    idem_key = f"tool:t2i:{current_user.id}:{body.prompt[:80]}:{body.aspect_ratio}"
    await _charge(db, current_user, _COST_TEXT2IMG, "text-to-image", idem_key)

    job_id = await _enqueue("text-to-image", current_user.id, _COST_TEXT2IMG, {
        "prompt": body.prompt, "aspect_ratio": body.aspect_ratio, "cost_paise": _COST_TEXT2IMG,
    })
    return JobOut(job_id=job_id, cost_paise=_COST_TEXT2IMG)
