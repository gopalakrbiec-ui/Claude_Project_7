from __future__ import annotations

"""
Photo enhancement tools — all fal.ai endpoints, no account unlock needed.

- PhotoRestoreAdapter  : deblur + upscale old/low-quality photos (GFPGAN / CodeFormer)
- BgRemoveAdapter      : background removal, returns transparent PNG
- PhotoUpscaleAdapter  : configurable-scale upscale via clarity-upscaler.
                         Output pixel size = input size x scale factor — a
                         2000x3000 phone photo at 4x lands around 8000x12000
                         ("8K"-class); actual result always depends on the
                         source photo's own resolution, never a fixed canvas.
"""

import logging

import fal_client
import httpx

from app.core.retry import ProviderError, with_timeout

logger = logging.getLogger(__name__)

_RESTORE_MODEL = "fal-ai/clarity-upscaler"  # general photo enhance + upscale (works on any photo)
_BG_REMOVE_MODEL = "fal-ai/birefnet"    # background removal (confirmed working)
_UPSCALE_MODEL = "fal-ai/clarity-upscaler"  # same model, driven by a configurable scale factor


def _set_fal_key(api_key: str) -> None:
    import os
    os.environ["FAL_KEY"] = api_key


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


class PhotoRestoreAdapter:
    """
    Restore and enhance old/blurry/damaged photos.
    Uses face-enhancement + upscaling under the hood.
    """

    def __init__(self, api_key: str, *, cost_paise: int = 200, timeout_seconds: float = 90.0) -> None:
        _set_fal_key(api_key)
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds

    async def restore(self, *, image_url: str) -> tuple[bytes, int]:
        """Returns (enhanced_image_bytes, cost_paise)."""
        async def _run() -> tuple[bytes, int]:
            logger.info("PhotoRestoreAdapter: submitting restore")
            handler = await fal_client.submit_async(
                _RESTORE_MODEL,
                arguments={"image_url": image_url, "scale": 2, "creativity": 0.3, "resemblance": 0.9},
            )
            result = await handler.get()
            # clarity-upscaler returns {"image": {"url": "..."}}
            out_url = (
                (result.get("image") or {}).get("url")
                or result.get("output")
                or result.get("url")
            )
            if not out_url:
                raise ProviderError(f"clarity-upscaler: no image URL in result: {result}")
            return await _download(out_url), self._cost_paise

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="PhotoRestoreAdapter")


class BgRemoveAdapter:
    """
    Remove background from a photo, returning a transparent PNG.
    Uses BiRefNet — high-quality segmentation, works on portraits and objects.
    """

    def __init__(self, api_key: str, *, cost_paise: int = 150, timeout_seconds: float = 60.0) -> None:
        _set_fal_key(api_key)
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds

    async def remove_bg(self, *, image_url: str) -> tuple[bytes, int]:
        """Returns (png_bytes_with_transparency, cost_paise)."""
        async def _run() -> tuple[bytes, int]:
            logger.info("BgRemoveAdapter: submitting bg-remove")
            handler = await fal_client.submit_async(
                _BG_REMOVE_MODEL,
                arguments={"image_url": image_url},
            )
            result = await handler.get()
            out_url = (result.get("image") or {}).get("url") or result.get("url")
            if not out_url:
                raise ProviderError(f"birefnet: no image URL in result: {result}")
            return await _download(out_url), self._cost_paise

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="BgRemoveAdapter")


class PhotoUpscaleAdapter:
    """
    Configurable-scale upscale via clarity-upscaler. scale=2/4/6 roughly map
    to the app's "HD" / "Ultra (8-16K)" / "Max" tiers — actual output pixel
    dimensions are always input_size x scale, never a fixed target canvas.
    """

    def __init__(self, api_key: str, *, cost_paise: int = 150, timeout_seconds: float = 120.0) -> None:
        _set_fal_key(api_key)
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds

    async def upscale(self, *, image_url: str, scale: int = 4) -> tuple[bytes, int]:
        """Returns (upscaled_image_bytes, cost_paise)."""
        async def _run() -> tuple[bytes, int]:
            logger.info("PhotoUpscaleAdapter: submitting upscale via clarity-upscaler scale=%d", scale)
            handler = await fal_client.submit_async(
                _UPSCALE_MODEL,
                arguments={"image_url": image_url, "scale": scale, "creativity": 0.15, "resemblance": 0.95},
            )
            result = await handler.get()
            out_url = (
                (result.get("image") or {}).get("url")
                or result.get("output")
                or result.get("url")
            )
            if not out_url:
                raise ProviderError(f"clarity-upscaler: no image URL in result: {result}")
            return await _download(out_url), self._cost_paise

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="PhotoUpscaleAdapter")
