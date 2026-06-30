from __future__ import annotations

"""
OpenAI gpt-image-1 adapter.

Handles: text-to-image, image editing (inpainting), style transfer.
Used for AI filter, background replace, and text-to-image tools.
"""

import asyncio
import base64
import io
import logging
from typing import Literal

import httpx

from app.adapters.generation import GenerationOutput
from app.core.retry import ProviderError, with_timeout

logger = logging.getLogger(__name__)

_BASE = "https://api.openai.com/v1"

_SIZE_MAP = {
    "1:1":  "1024x1024",
    "9:16": "1024x1536",
    "16:9": "1536x1024",
    "4:3":  "1536x1024",
    "3:4":  "1024x1536",
}

_STYLE_PROMPTS: dict[str, str] = {
    "anime":        "transform into anime illustration style, vibrant colors, clean linework, Studio Ghibli quality",
    "sketch":       "transform into detailed pencil sketch, fine line art, graphite shading",
    "oil_painting": "transform into classical oil painting style, rich textures, impasto brushwork, Renaissance style",
    "cinematic":    "cinematic movie still, dramatic lighting, shallow depth of field, film color grade",
    "watercolour":  "soft watercolor painting, delicate washes, painterly edges, pastel palette",
    "comic":        "bold comic book illustration, thick outlines, halftone dots, pop art colors",
    "ghibli":       "Studio Ghibli anime art style, soft pastel colors, whimsical and magical atmosphere",
    "vintage":      "vintage 1970s photograph, warm film grain, faded colors, retro aesthetic",
    "bollywood":    "Bollywood movie poster style, vibrant saturated colors, dramatic lighting, Indian cinema aesthetic",
    "royal":        "royal portrait painting style, gold leaf accents, regal composition, museum-quality fine art",
}



def _ensure_png(data: bytes) -> bytes:
    """Convert image bytes to PNG if not already PNG."""
    from PIL import Image
    if data[:4] == b"\x89PNG":
        return data
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class OpenAIImageAdapter:
    """
    Text-to-image and image editing via OpenAI gpt-image-1.

    generate()     — text prompt → image (1024×1024 or 1024×1536)
    edit()         — image + prompt → edited image (inpainting)
    style_filter() — apply artistic style to an uploaded photo
    """

    def __init__(
        self,
        api_key: str,
        *,
        cost_paise: int = 200,
        timeout_seconds: float = 120.0,
        quality: Literal["low", "medium", "high", "auto"] = "medium",
    ) -> None:
        self._api_key = api_key
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds
        self._quality = quality

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
        }

    async def generate(self, prompt: str, *, aspect_ratio: str = "1:1") -> tuple[bytes, int]:
        """Generate an image from a text prompt. Returns (image_bytes, cost_paise)."""
        size = _SIZE_MAP.get(aspect_ratio, "1024x1024")

        async def _run() -> tuple[bytes, int]:
            logger.info("OpenAI generate: size=%s prompt=%s…", size, prompt[:60])
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.post(
                    f"{_BASE}/images/generations",
                    headers=self._headers(),
                    json={
                        "model": "gpt-image-1",
                        "prompt": prompt,
                        "n": 1,
                        "size": size,
                        "quality": self._quality,
                        "output_format": "png",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            image_bytes = await self._extract_bytes(data, client if not client.is_closed else None)
            logger.info("OpenAI generate: done, %d bytes", len(image_bytes))
            return image_bytes, self._cost_paise

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="OpenAIImageAdapter.generate")

    async def edit(
        self,
        image_bytes: bytes,
        prompt: str,
        *,
        mask_bytes: bytes | None = None,
    ) -> tuple[bytes, int]:
        """Edit an image using a text prompt (optionally with a mask for inpainting)."""

        async def _run() -> tuple[bytes, int]:
            logger.info("OpenAI edit: prompt=%s…", prompt[:60])

            # OpenAI edits requires PNG; convert if necessary
            png_bytes = _ensure_png(image_bytes)
            files: dict = {
                "model": (None, "gpt-image-1"),
                "prompt": (None, prompt),
                "n": (None, "1"),
                "quality": (None, self._quality),
                "image": ("image.png", png_bytes, "image/png"),
            }
            if mask_bytes:
                files["mask"] = ("mask.png", _ensure_png(mask_bytes), "image/png")

            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.post(
                    f"{_BASE}/images/edits",
                    headers=self._headers(),
                    files=files,
                )
                resp.raise_for_status()
                data = resp.json()

            image_out = await self._extract_bytes(data, None)
            logger.info("OpenAI edit: done, %d bytes", len(image_out))
            return image_out, self._cost_paise

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="OpenAIImageAdapter.edit")

    async def style_filter(self, image_bytes: bytes, style: str) -> tuple[bytes, int]:
        """Apply an artistic style to the given image."""
        style_desc = _STYLE_PROMPTS.get(style, style)
        prompt = (
            f"Apply this artistic transformation to the person in the photo: {style_desc}. "
            "Keep the person's face, body position, and pose identical. Only change the artistic style."
        )
        return await self.edit(image_bytes, prompt)

    async def bg_replace(self, image_bytes: bytes, bg_prompt: str) -> tuple[bytes, int]:
        """Replace the background behind the subject with an AI-generated scene."""
        prompt = (
            f"Replace ONLY the background with: {bg_prompt}. "
            "Keep the person/subject in the foreground completely unchanged — "
            "same pose, clothing, lighting on the subject. Only the background changes."
        )
        return await self.edit(image_bytes, prompt)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _extract_bytes(self, data: dict, _client) -> bytes:
        """Extract PNG bytes from either b64_json or url response."""
        items = data.get("data") or []
        if not items:
            raise ProviderError(f"OpenAI: no images in response: {data}")

        item = items[0]

        # b64_json response
        b64 = item.get("b64_json")
        if b64:
            return base64.b64decode(b64)

        # URL response — download it
        url = item.get("url")
        if url:
            async with httpx.AsyncClient(timeout=60.0) as dl:
                r = await dl.get(url)
                r.raise_for_status()
                return r.content

        raise ProviderError(f"OpenAI: no b64_json or url in response item: {item}")

    async def _fetch_image_bytes(self, url: str) -> bytes:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.content


class OpenAIGenerationAdapter:
    """
    Implements GenerationProvider using OpenAI gpt-image-1 (text-to-image).
    Drop-in replacement for fal.ai in the order/template pipeline.
    Set GEN_PROVIDER=openai in Railway env to activate.
    """

    def __init__(
        self,
        api_key: str,
        *,
        cost_paise: int = 250,
        timeout_seconds: float = 120.0,
        quality: Literal["low", "medium", "high", "auto"] = "medium",
        aspect_ratio: str = "9:16",
    ) -> None:
        self._adapter = OpenAIImageAdapter(
            api_key=api_key,
            cost_paise=cost_paise,
            timeout_seconds=timeout_seconds,
            quality=quality,
        )
        self._aspect_ratio = aspect_ratio

    async def generate(self, prompt: str) -> "GenerationOutput":
        from app.adapters.generation import GenerationOutput
        image_bytes, cost = await self._adapter.generate(prompt, aspect_ratio=self._aspect_ratio)
        return GenerationOutput(
            media_bytes=image_bytes,
            cost_paise=cost,
            provider_name="openai/gpt-image-1",
            media_type="image",
            model_id="gpt-image-1",
        )
