from __future__ import annotations

"""
Inspire endpoints — stock photo and video search for the Inspire bottom sheet.

GET /inspire              → search photos (Pexels) + videos (Pixabay), merged results
GET /inspire/photos       → photos only (Pexels)
GET /inspire/videos       → videos only (Pixabay)
GET /inspire/keywords     → preset keyword chips for the UI
POST /inspire/notify      → capture "notify me when ready" signups (stored in Redis)

All search results are cached in Redis keyed by (source, query, page) for
inspire_cache_ttl_seconds (default 1 hour) to respect API rate limits.

No authentication required — users should be able to browse before logging in.
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/inspire", tags=["inspire"])

# Preset keywords shown as chips in the Inspire bottom sheet.
# Ordered by expected popularity for the target audience (Indian weddings / events).
_KEYWORDS = [
    "wedding", "mehndi", "haldi", "sangeet", "engagement",
    "birthday", "baby shower", "anniversary",
    "bollywood", "rajasthani", "traditional",
    "flowers", "nature", "golden hour",
]


async def _get_redis():
    from app.core.redis import get_redis
    return get_redis()


def _cache_key(source: str, query: str, page: int) -> str:
    safe_q = query.strip().lower().replace(" ", "_")[:60]
    return f"inspire:{source}:{safe_q}:{page}"


async def _cached_search(cache_key: str, ttl: int, fetch_fn) -> list[dict]:
    redis = await _get_redis()
    raw = await redis.get(cache_key)
    if raw:
        return json.loads(raw)
    items = await fetch_fn()
    serialised = [item.to_dict() for item in items]
    await redis.set(cache_key, json.dumps(serialised), ex=ttl)
    return serialised


# ---------------------------------------------------------------------------
# Keyword chips
# ---------------------------------------------------------------------------

@router.get("/keywords")
async def get_keywords() -> dict:
    """Returns preset keyword chips for the Inspire search bar."""
    return {"keywords": _KEYWORDS}


# ---------------------------------------------------------------------------
# Photos (Pexels)
# ---------------------------------------------------------------------------

@router.get("/photos")
async def search_photos(
    q: str = Query(default="", description="Search query; empty returns curated photos"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=40),
) -> dict:
    """Search Pexels for stock photos."""
    from app.core.config import get_settings
    from app.adapters.inspire import PexelsPhotoAdapter

    settings = get_settings()
    if not settings.pexels_api_key:
        raise HTTPException(status_code=503, detail="Pexels API key not configured")

    adapter = PexelsPhotoAdapter(api_key=settings.pexels_api_key)
    ttl = settings.inspire_cache_ttl_seconds
    cache_key = _cache_key("pexels", q or "__curated__", page)

    async def fetch():
        if q.strip():
            return await adapter.search(q.strip(), page=page, per_page=per_page)
        return await adapter.curated(page=page, per_page=per_page)

    try:
        items = await _cached_search(cache_key, ttl, fetch)
    except Exception as exc:
        logger.exception("Pexels search failed: q=%r", q)
        raise HTTPException(status_code=502, detail=f"Photo search unavailable: {exc}") from exc

    return {"results": items, "page": page, "per_page": per_page, "source": "pexels"}


# ---------------------------------------------------------------------------
# Videos (Pixabay)
# ---------------------------------------------------------------------------

@router.get("/videos")
async def search_videos(
    q: str = Query(default="wedding", description="Search query"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=40),
) -> dict:
    """Search Pixabay for stock videos."""
    from app.core.config import get_settings
    from app.adapters.inspire import PixabayVideoAdapter

    settings = get_settings()
    if not settings.pixabay_api_key:
        raise HTTPException(status_code=503, detail="Pixabay API key not configured")

    adapter = PixabayVideoAdapter(api_key=settings.pixabay_api_key)
    ttl = settings.inspire_cache_ttl_seconds
    cache_key = _cache_key("pixabay", q, page)

    async def fetch():
        return await adapter.search(q.strip() or "wedding", page=page, per_page=per_page)

    try:
        items = await _cached_search(cache_key, ttl, fetch)
    except Exception as exc:
        logger.exception("Pixabay search failed: q=%r", q)
        raise HTTPException(status_code=502, detail=f"Video search unavailable: {exc}") from exc

    return {"results": items, "page": page, "per_page": per_page, "source": "pixabay"}


# ---------------------------------------------------------------------------
# Combined search (photos + videos merged, photos first)
# ---------------------------------------------------------------------------

@router.get("")
async def search_inspire(
    q: str = Query(default="", description="Search query"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=40),
    type: str = Query(default="all", description="all | photo | video"),
) -> dict:
    """
    Combined Inspire search — returns photos (Pexels) and/or videos (Pixabay).

    Results are interleaved: roughly 70% photos, 30% videos when type=all.
    """
    from app.core.config import get_settings
    from app.adapters.inspire import PexelsPhotoAdapter, PixabayVideoAdapter

    settings = get_settings()

    photo_items: list[dict] = []
    video_items: list[dict] = []

    if type in ("all", "photo") and settings.pexels_api_key:
        photo_per_page = per_page if type == "photo" else max(14, per_page - 6)
        adapter = PexelsPhotoAdapter(api_key=settings.pexels_api_key)
        cache_key = _cache_key("pexels", q or "__curated__", page)

        async def fetch_photos():
            if q.strip():
                return await adapter.search(q.strip(), page=page, per_page=photo_per_page)
            return await adapter.curated(page=page, per_page=photo_per_page)

        try:
            photo_items = await _cached_search(cache_key, settings.inspire_cache_ttl_seconds, fetch_photos)
        except Exception:
            logger.exception("Pexels failed in combined search q=%r", q)

    if type in ("all", "video") and settings.pixabay_api_key:
        video_per_page = per_page if type == "video" else min(6, per_page // 3)
        adapter_v = PixabayVideoAdapter(api_key=settings.pixabay_api_key)
        cache_key_v = _cache_key("pixabay", q or "wedding", page)

        async def fetch_videos():
            return await adapter_v.search(q.strip() or "wedding", page=page, per_page=video_per_page)

        try:
            video_items = await _cached_search(cache_key_v, settings.inspire_cache_ttl_seconds, fetch_videos)
        except Exception:
            logger.exception("Pixabay failed in combined search q=%r", q)

    # Interleave: insert a video after every 3 photos
    merged: list[dict] = []
    vi = 0
    for i, photo in enumerate(photo_items):
        merged.append(photo)
        if (i + 1) % 3 == 0 and vi < len(video_items):
            merged.append(video_items[vi])
            vi += 1
    merged.extend(video_items[vi:])

    return {
        "results": merged,
        "page": page,
        "per_page": per_page,
        "total_photos": len(photo_items),
        "total_videos": len(video_items),
    }


# ---------------------------------------------------------------------------
# Notify Me When Ready (captures signups before Inspire goes live)
# ---------------------------------------------------------------------------

class NotifyIn(BaseModel):
    user_id: int | None = None
    phone: str | None = None


@router.post("/notify")
async def notify_when_ready(body: NotifyIn) -> dict:
    """Record a 'notify me' signup in Redis. Deduplicated by user_id or phone."""
    if not body.user_id and not body.phone:
        return {"status": "ok"}

    redis = await _get_redis()
    identifier = str(body.user_id) if body.user_id else body.phone
    await redis.sadd("inspire:notify:signups", identifier)
    logger.info("Inspire notify signup: %s", identifier)
    return {"status": "ok", "message": "You'll be notified when Inspire launches!"}
