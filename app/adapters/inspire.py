from __future__ import annotations

"""
Inspire feature adapters.

PexelsPhotoAdapter  — searches Pexels for stock photos
PixabayVideoAdapter — searches Pixabay for stock videos

Both return a list of InspireItem dicts ready to be serialised to the client.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_PEXELS_BASE = "https://api.pexels.com/v1"
_PIXABAY_BASE = "https://pixabay.com/api/videos"


class InspireItem:
    """Normalised result item returned to Flutter."""
    __slots__ = ("id", "type", "thumb_url", "preview_url", "full_url", "author", "source")

    def __init__(
        self,
        *,
        id: str,
        type: str,           # "photo" | "video"
        thumb_url: str,
        preview_url: str,
        full_url: str,
        author: str,
        source: str,         # "pexels" | "pixabay"
    ) -> None:
        self.id = id
        self.type = type
        self.thumb_url = thumb_url
        self.preview_url = preview_url
        self.full_url = full_url
        self.author = author
        self.source = source

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "thumb_url": self.thumb_url,
            "preview_url": self.preview_url,
            "full_url": self.full_url,
            "author": self.author,
            "source": self.source,
        }


class PexelsPhotoAdapter:
    """Search Pexels for stock photos."""

    def __init__(self, api_key: str, timeout: float = 10.0) -> None:
        self._headers = {"Authorization": api_key}
        self._timeout = timeout

    async def search(self, query: str, *, page: int = 1, per_page: int = 20) -> list[InspireItem]:
        params = {"query": query, "page": page, "per_page": per_page, "orientation": "portrait"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{_PEXELS_BASE}/search", headers=self._headers, params=params)
            resp.raise_for_status()
            data = resp.json()

        items: list[InspireItem] = []
        for photo in data.get("photos", []):
            src = photo.get("src", {})
            items.append(InspireItem(
                id=f"pexels-{photo['id']}",
                type="photo",
                thumb_url=src.get("tiny", ""),
                preview_url=src.get("medium", ""),
                full_url=src.get("large2x", src.get("original", "")),
                author=photo.get("photographer", ""),
                source="pexels",
            ))
        logger.info("Pexels search q=%r page=%d → %d results", query, page, len(items))
        return items

    async def curated(self, *, page: int = 1, per_page: int = 20) -> list[InspireItem]:
        """Fallback for empty-query browse (shows curated Pexels photos)."""
        params = {"page": page, "per_page": per_page}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{_PEXELS_BASE}/curated", headers=self._headers, params=params)
            resp.raise_for_status()
            data = resp.json()

        items: list[InspireItem] = []
        for photo in data.get("photos", []):
            src = photo.get("src", {})
            items.append(InspireItem(
                id=f"pexels-{photo['id']}",
                type="photo",
                thumb_url=src.get("tiny", ""),
                preview_url=src.get("medium", ""),
                full_url=src.get("large2x", src.get("original", "")),
                author=photo.get("photographer", ""),
                source="pexels",
            ))
        return items


class PixabayVideoAdapter:
    """Search Pixabay for stock videos."""

    def __init__(self, api_key: str, timeout: float = 10.0) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def search(self, query: str, *, page: int = 1, per_page: int = 20) -> list[InspireItem]:
        params = {
            "key": self._api_key,
            "q": query,
            "page": page,
            "per_page": per_page,
            "video_type": "film",
            "safesearch": "true",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(_PIXABAY_BASE, params=params)
            resp.raise_for_status()
            data = resp.json()

        items: list[InspireItem] = []
        for hit in data.get("hits", []):
            videos = hit.get("videos", {})
            tiny = videos.get("tiny", {})
            medium = videos.get("medium", {})
            large = videos.get("large", medium)
            items.append(InspireItem(
                id=f"pixabay-{hit['id']}",
                type="video",
                thumb_url=hit.get("userImageURL", tiny.get("thumbnail", "")),
                preview_url=tiny.get("url", ""),
                full_url=medium.get("url", large.get("url", "")),
                author=hit.get("user", ""),
                source="pixabay",
            ))
        logger.info("Pixabay search q=%r page=%d → %d results", query, page, len(items))
        return items
