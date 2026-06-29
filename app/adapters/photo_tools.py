from __future__ import annotations

"""
Photo enhancement tools — all fal.ai endpoints, no account unlock needed.

- PhotoRestoreAdapter  : deblur + upscale old/low-quality photos (GFPGAN / CodeFormer)
- BgRemoveAdapter      : background removal, returns transparent PNG
- PhotoUpscaleAdapter  : 4× upscale via Real-ESRGAN
"""

import logging

import fal_client
import httpx

from app.core.retry import ProviderError, with_timeout

logger = logging.getLogger(__name__)

_RESTORE_MODEL = "fal-ai/gfpgan"         # face restore + enhance
_BG_REMOVE_MODEL = "fal-ai/birefnet"    # background removal (confirmed working)
_UPSCALE_MODEL = "fal-ai/aura-sr"       # 4x upscaler (confirmed working)


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
                arguments={"image_url": image_url},
            )
            result = await handler.get()
            # gfpgan returns {"output": "url"} or {"image": {"url": ...}}
            out_url = (
                result.get("output")
                or (result.get("image") or {}).get("url")
                or result.get("url")
            )
            if not out_url:
                raise ProviderError(f"gfpgan: no image URL in result: {result}")
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
    4× upscale via Real-ESRGAN. Good for low-res source photos.
    """

    def __init__(self, api_key: str, *, cost_paise: int = 150, timeout_seconds: float = 60.0) -> None:
        _set_fal_key(api_key)
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds

    async def upscale(self, *, image_url: str, scale: int = 4) -> tuple[bytes, int]:
        """Returns (upscaled_image_bytes, cost_paise). scale param kept for API compat."""
        async def _run() -> tuple[bytes, int]:
            logger.info("PhotoUpscaleAdapter: submitting upscale via aura-sr")
            handler = await fal_client.submit_async(
                _UPSCALE_MODEL,
                arguments={"image_url": image_url},
            )
            result = await handler.get()
            out_url = (result.get("image") or {}).get("url") or result.get("url")
            if not out_url:
                raise ProviderError(f"aura-sr: no image URL in result: {result}")
            return await _download(out_url), self._cost_paise

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="PhotoUpscaleAdapter")
