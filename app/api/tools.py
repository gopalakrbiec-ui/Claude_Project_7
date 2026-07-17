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

# credit_ledger.idempotency_key is VARCHAR(128). Long R2 keys (photo-merge
# joins 2-4 of them) can push raw concatenation past that limit, which
# Postgres rejects with an uncaught DataError → bare 500 before the job
# is even enqueued. Hash anything that could exceed a safe margin.
_IDEM_KEY_MAX = 120


def _safe_idem_key(raw: str) -> str:
    if len(raw) <= _IDEM_KEY_MAX:
        return raw
    import hashlib
    prefix = raw.split(":")[0]
    digest = hashlib.sha256(raw.encode()).hexdigest()[:32]
    return f"{prefix}:h:{digest}"


# ---------------------------------------------------------------------------
# Tool discovery — Flutter reads this to build the center AI button grid
# ---------------------------------------------------------------------------


# cost_paise below = (provider cost + est. infra overhead) x 1.10 margin,
# assuming ~2,000 generations/month. Re-derive if real volume differs a lot —
# infra overhead per generation shrinks fast at higher volume.
_TOOL_CATALOG = [
    {"id": "ai-filter",       "name": "AI Filter",        "icon": "auto_awesome",  "cost_paise": 700,  "category": "style"},
    {"id": "ai-background",   "name": "AI Background",    "icon": "landscape",     "cost_paise": 700,  "category": "edit"},
    {"id": "ai-outfit",       "name": "AI Outfit",        "icon": "checkroom",     "cost_paise": 900,  "category": "fashion"},
    {"id": "hair-salon",      "name": "Hair Salon",       "icon": "content_cut",   "cost_paise": 700,  "category": "fashion"},
    {"id": "remix",           "name": "Remix",            "icon": "shuffle",       "cost_paise": 1000, "category": "creative"},
    {"id": "text-to-image",   "name": "Text to Image",   "icon": "text_fields",   "cost_paise": 700,  "category": "creative"},
    {"id": "photo-merge",     "name": "Photo Merge",     "icon": "group",         "cost_paise": 1100, "category": "creative",
     "keywords": ["together", "hugging", "kissing", "collage", "side by side", "wedding", "romantic", "friends"]},
    {"id": "photo-upscale",   "name": "Photo Upscale",   "icon": "high_quality",  "cost_paise": 2500, "category": "edit",
     "tiers": [
         {"key": "hd",    "label": "HD (2x)",         "cost_paise": 2500},
         {"key": "ultra", "label": "Ultra (4x, ~8-16K)", "cost_paise": 9000},
         {"key": "max",   "label": "Max (6x)",        "cost_paise": 18000},
     ]},
    {"id": "kling-video",     "name": "Kling-video",     "icon": "play_circle",   "cost_paise": 4300, "category": "video"},
    {"id": "wan-video",       "name": "Wan-video",       "icon": "play_circle",   "cost_paise": 2200, "category": "video"},
    {"id": "seedance-video",  "name": "Seedance-video",  "icon": "play_circle",   "cost_paise": 2000, "category": "video"},
    {"id": "veo-video",       "name": "Veo-video",       "icon": "play_circle",   "cost_paise": 4900, "category": "video"},
]


@router.get("", summary="List all available AI tools")
async def list_tools() -> dict:
    """Returns the catalog of AI tools for the center button grid in Flutter."""
    return {"tools": _TOOL_CATALOG}


# Cost per tool in paise — kept in sync with _TOOL_CATALOG above
_COST_AI_FILTER = 700
_COST_TRYON = 900
_COST_HAIR = 700
_COST_BG_REPLACE = 700
_COST_REMIX = 1000
_COST_TEXT2IMG = 700
_COST_PHOTO_MERGE = 1100

# Upscale cost is estimate-based (clarity-upscaler is compute/megapixel
# priced on fal.ai) — verify against real billing after first runs and
# adjust. Output pixel size = input size x scale, never a fixed canvas.
_UPSCALE_TIER_COST: dict[str, int] = {"hd": 2500, "ultra": 9000, "max": 18000}
_UPSCALE_TIER_SCALE: dict[str, int] = {"hd": 2, "ultra": 4, "max": 6}

_PHOTO_MERGE_KEYWORDS: dict[str, str] = {
    "together":    "Place all the people together in one natural photo, standing or sitting close together, smiling.",
    "hugging":     "Show all the people hugging each other warmly in one natural photo.",
    "kissing":     "Show the two people sharing a romantic kiss in one natural photo.",
    "collage":     "Create a beautiful photo collage arranging all the photos in an artistic layout.",
    "side by side": "Place the people side by side in one natural photo.",
    "wedding":     "Show all the people together in a beautiful wedding setting, elegantly dressed.",
    "romantic":    "Create a romantic scene with the people together, soft lighting and warm atmosphere.",
    "friends":     "Show all the people together as friends, laughing and having fun.",
}

# Video tool costs (paise)
_VIDEO_COSTS: dict[str, int] = {
    "kling-video":    4300,
    "wan-video":      2200,
    "seedance-video": 2000,
    "veo-video":      4900,
}


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


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
    keyword_tags: list[str] = Field(default_factory=list, max_length=30)

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
        base = self.prompt or self.style or self.remix_style or "creative remix"
        if self.keyword_tags:
            from app.core.prompt_keywords import build_prompt_fragment
            fragment = build_prompt_fragment(self.keyword_tags)
            if fragment:
                return f"{base}, {fragment}".strip(", ")
        return base


class TextToImageIn(BaseModel):
    prompt: str = Field(default="", max_length=500, description="Describe the image you want to generate")
    keyword_tags: list[str] = Field(default_factory=list, max_length=30)
    aspect_ratio: str = Field(default="9:16", description="9:16 | 1:1 | 16:9 | 4:3 | 3:4")

    @property
    def resolved_prompt(self) -> str:
        parts = [self.prompt.strip()] if self.prompt.strip() else []
        if self.keyword_tags:
            from app.core.prompt_keywords import build_prompt_fragment
            fragment = build_prompt_fragment(self.keyword_tags)
            if fragment:
                parts.append(fragment)
        return ", ".join(parts)


class PhotoMergeIn(BaseModel):
    photo_keys: list[str] = Field(..., min_length=2, max_length=4, description="2–4 R2 keys of photos to merge")
    keywords: list[str] = Field(default=[], description="Preset keywords: together | hugging | kissing | collage | side by side | wedding | romantic | friends")
    prompt: str = Field(default="", max_length=400, description="Optional free-text to add to the scene description")


class PhotoUpscaleIn(BaseModel):
    photo_key: str = Field(..., description="R2 key of the photo to upscale")
    tier: str = Field(default="ultra", description="hd (2x) | ultra (4x, ~8-16K depending on source) | max (6x)")


class AnimatePhotoIn(BaseModel):
    photo_key: str | None = Field(default=None, description="R2 key of the photo to animate; omit for text-to-video")
    prompt: str = Field(default="gentle motion, cinematic", max_length=300, description="Motion/scene description")
    duration: str = Field(default="5", description="Clip length in seconds: 5 or 10")
    aspect_ratio: str = Field(default="9:16", description="9:16 | 16:9 | 1:1")


_VIDEO_TOOL_IDS = set(_VIDEO_COSTS.keys())


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
    """Enqueue an arq run_tool job, record it in per-user history, and return the job_id."""
    import arq
    import time
    from app.core.config import get_settings
    from app.core.redis import get_redis
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

    # Record in per-user sorted set (score = unix timestamp) for history endpoint.
    # Keeps last 90 days; capped at 200 entries per user.
    redis = get_redis()
    user_key = f"tool:user:{user_id}:jobs"
    meta = json.dumps({"job_id": job_id, "tool_name": tool_name, "cost_paise": cost_paise})
    await redis.zadd(user_key, {meta: time.time()})
    await redis.zremrangebyrank(user_key, 0, -201)   # keep newest 200
    await redis.expire(user_key, 90 * 86400)

    return job_id


# ---------------------------------------------------------------------------
# Status polling
# ---------------------------------------------------------------------------


@router.get("/status/{job_id}", response_model=JobStatusOut)
async def tool_status(
    job_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
) -> JobStatusOut:
    """Poll the status of an async tool job. Only the job owner can see it."""
    from app.core.redis import get_redis

    redis = get_redis()
    raw = await redis.get(f"tool:job:{job_id}")
    if raw is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    data = json.loads(raw)

    # Ownership check — user_id is written into the record by the worker
    job_user_id = data.get("user_id")
    if job_user_id is not None and int(job_user_id) != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    return JobStatusOut(
        job_id=job_id,
        status=data.get("status", "processing"),
        cost_paise=data.get("cost_paise"),
        result_url=data.get("result_url"),
        error=data.get("error"),
    )


@router.get("/jobs", summary="List current user's tool job history")
async def list_tool_jobs(
    current_user: Annotated[User, Depends(get_current_user)],
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """
    Returns the authenticated user's tool job history, newest first.
    Merges per-job status from Redis so result_url is included when available.
    """
    from app.core.redis import get_redis

    redis = get_redis()
    user_key = f"tool:user:{current_user.id}:jobs"

    # Sorted set is scored by timestamp ascending; fetch newest first
    offset = (page - 1) * page_size
    raw_entries = await redis.zrevrange(user_key, offset, offset + page_size - 1)

    jobs = []
    for raw in raw_entries:
        meta = json.loads(raw)
        job_id = meta["job_id"]
        job_raw = await redis.get(f"tool:job:{job_id}")
        job_data: dict = json.loads(job_raw) if job_raw else {}
        jobs.append({
            "job_id": job_id,
            "type": "tool_job",
            "tool_name": job_data.get("tool_name") or meta["tool_name"],
            "status": job_data.get("status", "expired"),
            "result_url": job_data.get("result_url"),
            "error": job_data.get("error"),
            "cost_paise": meta["cost_paise"],
            "created_at": job_data.get("created_at"),
        })

    return {"jobs": jobs, "page": page, "page_size": page_size}


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
    idem_key = _safe_idem_key(f"tool:ai-filter:{current_user.id}:{body.photo_key}:{body.style}")
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

    idem_key = _safe_idem_key(f"tool:ai-outfit:{current_user.id}:{person_key}:{(body.garment_image_url or garment_key or '')[:64]}")
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

    idem_key = _safe_idem_key(f"tool:hair:{current_user.id}:{body.photo_key}:{body.hair_colour}:{body.hair_style_image_url}")
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
    idem_key = _safe_idem_key(f"tool:ai-bg:{current_user.id}:{body.photo_key}:{body.prompt[:64]}")
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

    idem_key = _safe_idem_key(f"tool:remix:{current_user.id}:{person_key}:{body.resolved_prompt[:64]}")
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
    """Generate an image from a text prompt and/or tap-to-build keyword tags."""
    prompt = body.resolved_prompt
    if not prompt:
        raise HTTPException(status_code=422, detail="Provide a prompt or at least one keyword tag")

    idem_key = _safe_idem_key(f"tool:t2i:{current_user.id}:{prompt[:80]}:{body.aspect_ratio}")
    await _charge(db, current_user, _COST_TEXT2IMG, "text-to-image", idem_key)

    job_id = await _enqueue("text-to-image", current_user.id, _COST_TEXT2IMG, {
        "prompt": prompt, "aspect_ratio": body.aspect_ratio, "cost_paise": _COST_TEXT2IMG,
    })
    return JobOut(job_id=job_id, cost_paise=_COST_TEXT2IMG)


# ---------------------------------------------------------------------------
# Photo Merge
# ---------------------------------------------------------------------------


@router.post("/photo-merge", response_model=JobOut)
async def photo_merge(
    body: PhotoMergeIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    """
    Merge 2–4 photos into one AI-generated composite scene.

    Keywords auto-build a scene prompt; free-text `prompt` is appended.
    Keyword options: together | hugging | kissing | collage | side by side | wedding | romantic | friends
    """
    idem_key = _safe_idem_key(f"tool:photo-merge:{current_user.id}:{':'.join(sorted(body.photo_keys))}:{','.join(sorted(body.keywords))}")
    await _charge(db, current_user, _COST_PHOTO_MERGE, "photo-merge", idem_key)

    # Build scene description from keywords + free text
    keyword_descs = [
        _PHOTO_MERGE_KEYWORDS[kw.lower().strip()]
        for kw in body.keywords
        if kw.lower().strip() in _PHOTO_MERGE_KEYWORDS
    ]
    scene = " ".join(keyword_descs)
    if body.prompt.strip():
        scene = f"{scene} {body.prompt.strip()}".strip()
    if not scene:
        scene = "Place all the people together in one natural, beautiful photo."

    job_id = await _enqueue("photo-merge", current_user.id, _COST_PHOTO_MERGE, {
        "photo_keys": body.photo_keys,
        "scene": scene,
        "cost_paise": _COST_PHOTO_MERGE,
    })
    return JobOut(job_id=job_id, cost_paise=_COST_PHOTO_MERGE)


# ---------------------------------------------------------------------------
# Photo Upscale
# ---------------------------------------------------------------------------


@router.post("/photo-upscale", response_model=JobOut)
async def photo_upscale(
    body: PhotoUpscaleIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    """
    Upscale a photo. Output pixel size = input size x scale factor for the
    chosen tier — never a fixed target resolution. A low-res source photo
    will not become genuinely sharp 16K detail, just a larger version of
    itself; tiers are named for the typical result from a modern phone photo.
    """
    tier = body.tier.lower().strip()
    if tier not in _UPSCALE_TIER_COST:
        raise HTTPException(status_code=422, detail="tier must be one of: hd, ultra, max")

    cost = _UPSCALE_TIER_COST[tier]
    idem_key = _safe_idem_key(f"tool:photo-upscale:{current_user.id}:{body.photo_key}:{tier}")
    await _charge(db, current_user, cost, "photo-upscale", idem_key)

    job_id = await _enqueue("photo-upscale", current_user.id, cost, {
        "photo_key": body.photo_key,
        "scale": _UPSCALE_TIER_SCALE[tier],
        "cost_paise": cost,
    })
    return JobOut(job_id=job_id, cost_paise=cost)


# ---------------------------------------------------------------------------
# Animate Photo
# ---------------------------------------------------------------------------


@router.post("/animate-photo", response_model=JobOut)
async def animate_photo(
    body: AnimatePhotoIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    """Animate a still photo — defaults to Kling v2.1. Kept for backwards compatibility."""
    return await _run_video_tool("kling-video", body, current_user, db)


@router.post("/{video_tool_id}", response_model=JobOut)
async def run_video_tool(
    video_tool_id: str,
    body: AnimatePhotoIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobOut:
    """Unified video tool endpoint — accepts kling-video, wan-video, seedance-video, veo-video."""
    if video_tool_id not in _VIDEO_TOOL_IDS:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {video_tool_id}")
    return await _run_video_tool(video_tool_id, body, current_user, db)


async def _run_video_tool(
    tool_id: str,
    body: AnimatePhotoIn,
    current_user: User,
    db: AsyncSession,
) -> JobOut:
    cost = _VIDEO_COSTS[tool_id]
    if not body.photo_key and not body.prompt.strip():
        raise HTTPException(status_code=422, detail="Provide a photo, a prompt, or both")
    idem_key = _safe_idem_key(f"tool:{tool_id}:{current_user.id}:{body.photo_key or body.prompt[:64]}:{body.duration}")
    await _charge(db, current_user, cost, tool_id, idem_key)
    job_id = await _enqueue(tool_id, current_user.id, cost, {
        "photo_key": body.photo_key,
        "prompt": body.prompt,
        "duration": body.duration,
        "aspect_ratio": body.aspect_ratio,
        "cost_paise": cost,
    })
    return JobOut(job_id=job_id, cost_paise=cost)
