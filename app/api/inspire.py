from __future__ import annotations

"""
Inspire endpoints — Pexels stock photo search for the Inspire bottom sheet.

GET /inspire         → search photos; curated feed when query is empty
GET /inspire/photos  → explicit alias for the same
GET /inspire/keywords → preset keyword chips for the UI

Results cached in Redis for inspire_cache_ttl_seconds (default 1 hour).
No authentication required.
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/inspire", tags=["inspire"])

# Preset keywords shown as chips in the Inspire bottom sheet.
# Ordered by expected popularity for the target audience (Indian weddings / events).
_KEYWORDS = [
    "wedding", "mehndi", "haldi", "sangeet", "engagement",
    "birthday", "baby shower", "anniversary",
    "bollywood", "rajasthani", "traditional",
    "flowers", "nature", "golden hour",
    "wedding dress", "reception dress", "mens fashion", "womens fashion",
    "personal grooming", "new outfit ideas",
]

# Categories for the pre-generated AI styling gallery (see /inspire/styled).
# Shared with scripts/generate_inspire_gallery.py — keep both in sync.
STYLE_GALLERY_CATEGORIES = [
    "celebrity-styling", "billionaire-styling", "actress-dressing",
    "wedding-dress", "reception-dress", "mens-grooming", "womens-fashion",
    "new-outfit-ideas",
]


def _cache_key(source: str, query: str, page: int) -> str:
    safe_q = query.strip().lower().replace(" ", "_")[:60]
    return f"inspire:{source}:{safe_q}:{page}"


async def _cached_search(cache_key: str, ttl: int, fetch_fn) -> list[dict]:
    from app.core.redis import get_redis
    redis = get_redis()
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
# Photos — blends Unsplash (better fashion/portrait quality) + Pexels
# ---------------------------------------------------------------------------

async def _fetch_blended_photos(q: str, page: int, per_page: int) -> tuple[list[dict], str]:
    """
    Queries Unsplash and Pexels concurrently (whichever keys are configured)
    and interleaves results, Unsplash first. Falls back to whichever single
    source is available. Raises if neither is configured.
    """
    import asyncio
    from app.core.config import get_settings
    from app.adapters.inspire import PexelsPhotoAdapter, UnsplashPhotoAdapter

    settings = get_settings()
    if not settings.pexels_api_key and not settings.unsplash_access_key:
        raise HTTPException(status_code=503, detail="No Inspire photo provider configured")

    half = max(1, per_page // 2) if (settings.pexels_api_key and settings.unsplash_access_key) else per_page

    async def fetch_unsplash() -> list:
        if not settings.unsplash_access_key:
            return []
        adapter = UnsplashPhotoAdapter(access_key=settings.unsplash_access_key)
        if q.strip():
            return await adapter.search(q.strip(), page=page, per_page=half)
        return await adapter.curated(page=page, per_page=half)

    async def fetch_pexels() -> list:
        if not settings.pexels_api_key:
            return []
        adapter = PexelsPhotoAdapter(api_key=settings.pexels_api_key)
        if q.strip():
            return await adapter.search(q.strip(), page=page, per_page=half)
        return await adapter.curated(page=page, per_page=half)

    unsplash_items, pexels_items = await asyncio.gather(fetch_unsplash(), fetch_pexels())
    combined = [item.to_dict() for item in [*unsplash_items, *pexels_items]]

    if settings.unsplash_access_key and settings.pexels_api_key:
        source = "unsplash+pexels"
    elif settings.unsplash_access_key:
        source = "unsplash"
    else:
        source = "pexels"

    return combined, source


@router.get("/photos")
async def search_photos(
    q: str = Query(default="", description="Search query; empty returns curated photos"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=40),
) -> dict:
    """Search for stock photos — blends Unsplash + Pexels when both are configured."""
    from app.core.config import get_settings

    settings = get_settings()
    cache_key = _cache_key("blend", q or "__curated__", page)

    async def fetch_and_cache() -> list[dict]:
        from app.core.redis import get_redis
        redis = get_redis()
        raw = await redis.get(cache_key)
        if raw:
            return json.loads(raw)
        items, _ = await _fetch_blended_photos(q, page, per_page)
        await redis.set(cache_key, json.dumps(items), ex=settings.inspire_cache_ttl_seconds)
        return items

    try:
        items = await fetch_and_cache()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Inspire photo search failed: q=%r", q)
        raise HTTPException(status_code=502, detail=f"Photo search unavailable: {exc}") from exc

    return {"results": items, "page": page, "per_page": per_page}


# ---------------------------------------------------------------------------
# Combined search — alias of /photos
# ---------------------------------------------------------------------------

@router.get("")
async def search_inspire(
    q: str = Query(default="", description="Search query; empty returns curated photos"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=40),
) -> dict:
    """Inspire search — same as /inspire/photos. Curated feed when query is empty."""
    return await search_photos(q=q, page=page, per_page=per_page)


# ---------------------------------------------------------------------------
# AI-generated styling gallery — celebrity/billionaire/actress styling etc.
# Pre-generated once via scripts/generate_inspire_gallery.py, stored in R2.
# Sidesteps celebrity-photo licensing/publicity-rights issues entirely since
# every image is originally generated, not a real photo.
# ---------------------------------------------------------------------------

@router.get("/styled")
async def get_styled_gallery(
    category: str = Query(default="", description="Filter by category; empty returns all"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=40),
) -> dict:
    """Returns pre-generated AI styling inspiration images (no live API call)."""
    from sqlalchemy import select
    from app.core.database import get_session_factory
    from app.models.inspire_gallery import InspireGalleryItem

    factory = get_session_factory()
    offset = (page - 1) * per_page

    async with factory() as session:
        stmt = select(InspireGalleryItem).where(InspireGalleryItem.active.is_(True))
        if category.strip():
            stmt = stmt.where(InspireGalleryItem.category == category.strip().lower())
        stmt = stmt.order_by(InspireGalleryItem.id.desc()).offset(offset).limit(per_page)
        rows = (await session.execute(stmt)).scalars().all()

    results = [
        {
            "id": f"styled-{row.id}",
            "type": "photo",
            "category": row.category,
            "thumb_url": row.image_url,
            "preview_url": row.image_url,
            "full_url": row.image_url,
            "author": "Savi Nenapu",
            "source": "ai-styled",
        }
        for row in rows
    ]
    return {"results": results, "page": page, "per_page": per_page}


@router.get("/styled/categories")
async def get_styled_categories() -> dict:
    """Returns the fixed category list for the AI-generated styling gallery."""
    return {"categories": STYLE_GALLERY_CATEGORIES}


