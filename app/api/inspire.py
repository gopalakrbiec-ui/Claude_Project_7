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
]


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
# Combined search — Pexels photos only for now
# ---------------------------------------------------------------------------

@router.get("")
async def search_inspire(
    q: str = Query(default="", description="Search query; empty returns curated photos"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=40),
) -> dict:
    """Inspire search — returns Pexels stock photos. Curated feed when query is empty."""
    from app.core.config import get_settings
    from app.adapters.inspire import PexelsPhotoAdapter

    settings = get_settings()
    if not settings.pexels_api_key:
        raise HTTPException(status_code=503, detail="Pexels API key not configured")

    adapter = PexelsPhotoAdapter(api_key=settings.pexels_api_key)
    cache_key = _cache_key("pexels", q or "__curated__", page)

    async def fetch():
        if q.strip():
            return await adapter.search(q.strip(), page=page, per_page=per_page)
        return await adapter.curated(page=page, per_page=per_page)

    try:
        items = await _cached_search(cache_key, settings.inspire_cache_ttl_seconds, fetch)
    except Exception as exc:
        logger.exception("Pexels search failed: q=%r", q)
        raise HTTPException(status_code=502, detail=f"Search unavailable: {exc}") from exc

    return {"results": items, "page": page, "per_page": per_page, "source": "pexels"}


