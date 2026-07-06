from __future__ import annotations

"""
OpenAI gpt-image-2 adapter.

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



def _raise_with_body(resp: httpx.Response, label: str) -> None:
    """
    Like resp.raise_for_status(), but includes OpenAI's JSON error body in the
    exception message. Bare HTTPStatusError only shows "400 Bad Request" with
    no indication of *why* — OpenAI's body has the actual reason (bad param,
    image too small/large, unsupported count, etc.) and is essential for
    diagnosing failures from worker logs alone.
    """
    if resp.is_success:
        return
    try:
        body = resp.json()
    except Exception:
        body = resp.text[:500]
    logger.error("OpenAI %s error %s: %s", label, resp.status_code, body)
    raise ProviderError(f"OpenAI {label} failed ({resp.status_code}): {body}")


_MIN_TOTAL_PIXELS = 655_360      # OpenAI images/edits minimum (e.g. ~809x809)
_MAX_EDGE_PIXELS = 3840          # OpenAI images/edits maximum single edge


def _ensure_png(data: bytes) -> bytes:
    """
    Convert image bytes to PNG and normalize dimensions to satisfy OpenAI's
    images/edits constraints (655,360–8,294,400 total pixels, max edge 3840px).
    Garment/reference photos are often small thumbnails that violate the
    minimum, which OpenAI rejects with a bare 400 and no obvious cause.
    """
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    w, h = img.size

    total = w * h
    if total < _MIN_TOTAL_PIXELS:
        scale = (_MIN_TOTAL_PIXELS / total) ** 0.5
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    elif max(w, h) > _MAX_EDGE_PIXELS:
        scale = _MAX_EDGE_PIXELS / max(w, h)
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class OpenAIImageAdapter:
    """
    Text-to-image and image editing via OpenAI gpt-image-2.

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
        model: str = "gpt-image-2",
    ) -> None:
        self._api_key = api_key
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds
        self._quality = quality
        self._model = model

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
                        "model": self._model,
                        "prompt": prompt,
                        "n": 1,
                        "size": size,
                        "quality": self._quality,
                        "output_format": "png",
                    },
                )
                _raise_with_body(resp, "generate")
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
        return await self.edit_multi([image_bytes], prompt, mask_bytes=mask_bytes)

    async def edit_multi(
        self,
        images: list[bytes],
        prompt: str,
        *,
        mask_bytes: bytes | None = None,
    ) -> tuple[bytes, int]:
        """
        Edit with multiple input images (template + user photo).
        OpenAI composites all images guided by the prompt.
        images[0] = template/background, images[1] = user photo (face/person).
        """
        async def _run() -> tuple[bytes, int]:
            logger.info("OpenAI edit_multi: images=%d prompt=%s…", len(images), prompt[:60])

            # Build multipart — each image is a separate "image[]" field
            files: list[tuple[str, tuple]] = [
                ("model", (None, self._model)),
                ("prompt", (None, prompt)),
                ("n", (None, "1")),
                ("quality", (None, self._quality)),
            ]
            for i, img in enumerate(images):
                files.append(("image[]", (f"image_{i}.png", _ensure_png(img), "image/png")))
            if mask_bytes:
                files.append(("mask", ("mask.png", _ensure_png(mask_bytes), "image/png")))

            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.post(
                    f"{_BASE}/images/edits",
                    headers=self._headers(),
                    files=files,
                )
                _raise_with_body(resp, "edit_multi")
                data = resp.json()

            image_out = await self._extract_bytes(data, None)
            logger.info("OpenAI edit_multi: done, %d bytes", len(image_out))
            return image_out, self._cost_paise

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="OpenAIImageAdapter.edit_multi")

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
    Implements GenerationProvider using OpenAI gpt-image-2 (text-to-image + edit).
    Drop-in replacement for fal.ai in the order/template pipeline.
    Set GEN_PROVIDER=openai in Railway env to activate.

    When both a template image and user photo are available,
    generate_with_context() uses the edit API to composite the user's
    face directly into the template frame — much better than text-only generation.
    """

    def __init__(
        self,
        api_key: str,
        *,
        cost_paise: int = 250,
        timeout_seconds: float = 120.0,
        quality: Literal["low", "medium", "high", "auto"] = "medium",
        model: str = "gpt-image-2",
        aspect_ratio: str = "9:16",
    ) -> None:
        self._adapter = OpenAIImageAdapter(
            api_key=api_key,
            cost_paise=cost_paise,
            timeout_seconds=timeout_seconds,
            quality=quality,
            model=model,
        )
        self._model = model
        self._aspect_ratio = aspect_ratio

    async def generate(self, prompt: str) -> "GenerationOutput":
        from app.adapters.generation import GenerationOutput
        image_bytes, cost = await self._adapter.generate(prompt, aspect_ratio=self._aspect_ratio)
        return GenerationOutput(
            media_bytes=image_bytes,
            cost_paise=cost,
            provider_name=f"openai/{self._model}",
            media_type="image",
            model_id=self._model,
        )

    async def generate_with_context(
        self,
        prompt: str,
        *,
        template_image_url: str | None = None,
        face_image_url: str | None = None,
    ) -> "GenerationOutput":
        """
        Use the edit API with template + user photo when available.
        Falls back to text-only generate() if no images are provided.
        """
        from app.adapters.generation import GenerationOutput

        images: list[bytes] = []

        if template_image_url or face_image_url:
            async with httpx.AsyncClient(timeout=60.0) as client:
                if template_image_url:
                    r = await client.get(template_image_url)
                    r.raise_for_status()
                    images.append(r.content)
                if face_image_url:
                    r = await client.get(face_image_url)
                    r.raise_for_status()
                    images.append(r.content)

        if not images:
            return await self.generate(prompt)

        # Build compositing prompt: template is fixed background, user face is preserved exactly
        face_instruction = (
            "The second image is the user's photo — transplant their face and person into "
            "the portrait area of the template. "
            "CRITICAL: preserve the user's face, skin tone, and facial features 100% exactly "
            "as they appear in their photo. Do not beautify, alter, or replace the face. "
            "Match the lighting and shadows of the template scene around the face. "
        ) if face_image_url else ""

        composite_prompt = (
            f"{prompt}\n\n"
            "The first image is the template — keep its layout, decorative elements, "
            "text overlays, borders, and colour scheme exactly unchanged. "
            f"{face_instruction}"
            "Return only the final composited image, nothing else."
        )

        image_bytes, cost = await self._adapter.edit_multi(images, composite_prompt)
        return GenerationOutput(
            media_bytes=image_bytes,
            cost_paise=cost,
            provider_name=f"openai/{self._model}",
            media_type="image",
            model_id=self._model,
        )
