from __future__ import annotations

"""
AI creative tool adapters — all backed by fal.ai.

- StyleTransferAdapter   : apply artistic style (anime, sketch, oil-painting, etc.)
- VirtualTryOnAdapter    : dress a person in an outfit image (CatVTON)
- HairSalonAdapter       : change hair colour / style (HairFastGAN)
- AiBgReplaceAdapter     : replace background with an AI-generated scene
- RemixAdapter           : multi-image Remix — person + outfit + accessory → combined
- TextToImageAdapter     : free-form text → image (Flux Schnell via fal)
"""

import logging

import fal_client
import httpx

from app.core.retry import ProviderError, with_timeout

logger = logging.getLogger(__name__)

_STYLE_TRANSFER_MODEL = "fal-ai/flux/dev/image-to-image"   # confirmed working
_TRYON_MODEL = "fal-ai/cat-vton"                           # confirmed working
_HAIR_MODEL = "fal-ai/flux/dev/image-to-image"            # flux i2i — works on any photo, free text hair desc
_BG_REPLACE_MODEL = "fal-ai/bria/background-replace"      # correct slug with slash
_REMIX_MODEL = "fal-ai/flux/dev/image-to-image"           # confirmed working
_TEXT2IMG_MODEL = "fal-ai/flux/schnell"                   # confirmed working


def _set_fal_key(api_key: str) -> None:
    import os
    os.environ["FAL_KEY"] = api_key


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


def _first_image_url(result: dict) -> str:
    """Extract the first image URL from any fal.ai result shape."""
    for key in ("images", "image"):
        val = result.get(key)
        if isinstance(val, list) and val:
            url = val[0].get("url") if isinstance(val[0], dict) else val[0]
            if url:
                return url
        if isinstance(val, dict):
            url = val.get("url")
            if url:
                return url
    url = result.get("url") or result.get("output_url")
    if url:
        return url
    raise ProviderError(f"No image URL found in fal result: {list(result.keys())}")


# ---------------------------------------------------------------------------
# Style Transfer — anime / sketch / oil-painting / cinematic filters
# ---------------------------------------------------------------------------

_STYLE_PROMPTS: dict[str, str] = {
    "anime": "anime illustration, Studio Ghibli style, cel shading, vibrant colours",
    "sketch": "pencil sketch, charcoal drawing, artistic, detailed linework",
    "oil_painting": "oil painting, impressionist style, visible brushstrokes, rich colours",
    "cinematic": "cinematic film still, dramatic lighting, movie colour grade, 8K",
    "watercolour": "watercolour painting, soft washes, dreamy, artistic",
    "comic": "comic book style, bold outlines, halftone dots, pop art colours",
    "ghibli": "Studio Ghibli anime, soft pastel palette, warm magical realism",
    "vintage": "vintage film photograph, grain, faded colours, 1970s Kodachrome",
}


class StyleTransferAdapter:
    def __init__(self, api_key: str, *, cost_paise: int = 200, timeout_seconds: float = 120.0) -> None:
        _set_fal_key(api_key)
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds

    async def apply_style(self, *, image_url: str, style: str, strength: float = 0.75) -> tuple[bytes, int]:
        """
        style: one of the keys in _STYLE_PROMPTS, or a custom prompt string.
        strength: 0.0 = keep original, 1.0 = full style transformation.
        """
        style_prompt = _STYLE_PROMPTS.get(style, style)

        async def _run() -> tuple[bytes, int]:
            logger.info("StyleTransferAdapter: style=%s strength=%.2f", style, strength)
            handler = await fal_client.submit_async(
                _STYLE_TRANSFER_MODEL,
                arguments={
                    "image_url": image_url,
                    "prompt": style_prompt,
                    "strength": strength,
                    "num_inference_steps": 28,
                    "guidance_scale": 3.5,
                },
            )
            result = await handler.get()
            url = _first_image_url(result)
            return await _download(url), self._cost_paise

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="StyleTransferAdapter")


# ---------------------------------------------------------------------------
# Virtual Try-On — put user in an outfit image (CatVTON)
# ---------------------------------------------------------------------------


class VirtualTryOnAdapter:
    def __init__(self, api_key: str, *, cost_paise: int = 400, timeout_seconds: float = 150.0) -> None:
        _set_fal_key(api_key)
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds

    async def try_on(
        self,
        *,
        person_image_url: str,
        garment_image_url: str,
        category: str = "upper_body",  # "upper_body" | "lower_body" | "dresses"
    ) -> tuple[bytes, int]:
        async def _run() -> tuple[bytes, int]:
            logger.info("VirtualTryOnAdapter: category=%s", category)
            handler = await fal_client.submit_async(
                _TRYON_MODEL,
                arguments={
                    "human_image_url": person_image_url,
                    "garment_image_url": garment_image_url,
                    "category": category,
                },
            )
            result = await handler.get()
            url = _first_image_url(result)
            return await _download(url), self._cost_paise

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="VirtualTryOnAdapter")


# ---------------------------------------------------------------------------
# Hair Salon — change hair colour / style (HairFastGAN)
# ---------------------------------------------------------------------------

_HAIR_COLOURS: dict[str, str] = {
    "black": "#1a1a1a",
    "brown": "#8B4513",
    "blonde": "#F5DEB3",
    "red": "#B22222",
    "auburn": "#922B21",
    "grey": "#808080",
    "blue": "#4169E1",
    "pink": "#FF69B4",
    "purple": "#800080",
    "green": "#228B22",
}


class HairSalonAdapter:
    def __init__(self, api_key: str, *, cost_paise: int = 200, timeout_seconds: float = 90.0) -> None:
        _set_fal_key(api_key)
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds

    async def change_hair(
        self,
        *,
        image_url: str,
        hair_style_image_url: str | None = None,
        hair_colour: str | None = None,
    ) -> tuple[bytes, int]:
        """
        Uses flux image-to-image with a hair-change prompt.
        Accepts free text for hair_colour (e.g. "curly brown", "black wavy").
        """
        desc = hair_colour or "natural"
        if hair_style_image_url:
            desc = f"{desc} style matching the reference"
        prompt = (
            f"Change the hair to {desc}. Keep the person's face, clothing, pose, and background "
            "completely unchanged. Only the hair colour and style changes."
        )

        async def _run() -> tuple[bytes, int]:
            logger.info("HairSalonAdapter: prompt=%s", prompt[:80])
            handler = await fal_client.submit_async(
                _HAIR_MODEL,
                arguments={
                    "image_url": image_url,
                    "prompt": prompt,
                    "strength": 0.65,
                    "num_inference_steps": 28,
                    "guidance_scale": 3.5,
                },
            )
            result = await handler.get()
            url = _first_image_url(result)
            return await _download(url), self._cost_paise

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="HairSalonAdapter")


# ---------------------------------------------------------------------------
# AI Background Replace — swap background with an AI scene (BRIA)
# ---------------------------------------------------------------------------


class AiBgReplaceAdapter:
    def __init__(self, api_key: str, *, cost_paise: int = 200, timeout_seconds: float = 90.0) -> None:
        _set_fal_key(api_key)
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds

    async def replace_bg(
        self,
        *,
        image_url: str,
        prompt: str,
    ) -> tuple[bytes, int]:
        """
        Replace the background of image_url with a scene described by prompt.
        e.g. prompt = "Rajasthani palace courtyard at golden hour"
        """
        async def _run() -> tuple[bytes, int]:
            logger.info("AiBgReplaceAdapter: prompt=%r", prompt[:80])
            handler = await fal_client.submit_async(
                _BG_REPLACE_MODEL,
                arguments={
                    "image_url": image_url,
                    "prompt": prompt,
                },
            )
            result = await handler.get()
            url = _first_image_url(result)
            return await _download(url), self._cost_paise

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="AiBgReplaceAdapter")


# ---------------------------------------------------------------------------
# Remix — multi-image fusion (person + outfit + accessory)
# ---------------------------------------------------------------------------


class RemixAdapter:
    """
    Combines up to 3 reference images into one AI-generated output.
    Uses Flux Dev image-to-image with a merged reference collage.
    """

    def __init__(self, api_key: str, *, cost_paise: int = 500, timeout_seconds: float = 150.0) -> None:
        _set_fal_key(api_key)
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds

    async def remix(
        self,
        *,
        person_image_url: str,
        prompt: str,
        style_image_url: str | None = None,
        accessory_image_url: str | None = None,
        strength: float = 0.85,
    ) -> tuple[bytes, int]:
        """
        person_image_url  — the user's face/body photo
        style_image_url   — outfit or style reference
        accessory_image_url — jewellery, prop, or third reference
        prompt            — text describing the desired output
        """
        async def _run() -> tuple[bytes, int]:
            # Build a merged prompt that references all inputs
            full_prompt = prompt
            if style_image_url:
                full_prompt += ", wearing the exact outfit from the reference"
            if accessory_image_url:
                full_prompt += ", with the accessories shown in the reference"
            full_prompt += ". Ultra-realistic, 8K, cinematic lighting."

            logger.info("RemixAdapter: prompt=%r has_style=%s has_accessory=%s",
                        full_prompt[:80], bool(style_image_url), bool(accessory_image_url))

            handler = await fal_client.submit_async(
                _REMIX_MODEL,
                arguments={
                    "image_url": person_image_url,
                    "prompt": full_prompt,
                    "strength": strength,
                    "num_inference_steps": 28,
                    "guidance_scale": 3.5,
                },
            )
            result = await handler.get()
            url = _first_image_url(result)
            return await _download(url), self._cost_paise

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="RemixAdapter")


# ---------------------------------------------------------------------------
# Text to Image — free-form prompt → image (Flux Schnell)
# ---------------------------------------------------------------------------


class TextToImageAdapter:
    def __init__(self, api_key: str, *, cost_paise: int = 200, timeout_seconds: float = 60.0) -> None:
        _set_fal_key(api_key)
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds

    async def generate(
        self,
        *,
        prompt: str,
        aspect_ratio: str = "9:16",
    ) -> tuple[bytes, int]:
        _sizes = {
            "9:16": {"width": 768, "height": 1344},
            "1:1": {"width": 1024, "height": 1024},
            "16:9": {"width": 1344, "height": 768},
            "4:3": {"width": 1024, "height": 768},
            "3:4": {"width": 768, "height": 1024},
        }
        dims = _sizes.get(aspect_ratio, _sizes["9:16"])

        async def _run() -> tuple[bytes, int]:
            logger.info("TextToImageAdapter: prompt=%r aspect=%s", prompt[:80], aspect_ratio)
            handler = await fal_client.submit_async(
                _TEXT2IMG_MODEL,
                arguments={
                    "prompt": prompt,
                    "image_size": dims,
                    "num_inference_steps": 4,
                    "num_images": 1,
                },
            )
            result = await handler.get()
            url = _first_image_url(result)
            return await _download(url), self._cost_paise

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="TextToImageAdapter")
