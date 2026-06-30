from __future__ import annotations

"""
Face-swap adapter using fal-ai/face_swap.

Swaps the face from a source photo into a target template image.
No fal.ai account unlock required for this model (public endpoint).
"""

import logging

import fal_client
import httpx

from app.adapters.generation import GenerationOutput
from app.core.retry import ProviderError, with_timeout

logger = logging.getLogger(__name__)

_MODEL = "fal-ai/face-swap"


def _set_fal_key(api_key: str) -> None:
    import os
    os.environ["FAL_KEY"] = api_key


class FalFaceSwapAdapter:
    """
    Swap a user's face into a target scene/template image.

    source_image_url — user's uploaded photo (the face to transplant)
    target_image_url — template/style image (the body/scene)
    """

    def __init__(
        self,
        api_key: str,
        *,
        cost_paise: int = 300,
        timeout_seconds: float = 120.0,
    ) -> None:
        _set_fal_key(api_key)
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds

    async def swap(
        self,
        *,
        source_image_url: str,
        target_image_url: str,
    ) -> GenerationOutput:
        async def _run() -> GenerationOutput:
            logger.info("FalFaceSwapAdapter: submitting face swap")
            handler = await fal_client.submit_async(
                _MODEL,
                arguments={
                    "base_image_url": target_image_url,
                    "face_image_url": source_image_url,
                },
            )
            result = await handler.get()
            logger.info("FalFaceSwapAdapter: done request_id=%s", handler.request_id)

            images = result.get("images") or []
            if not images:
                image_url = result.get("image", {}).get("url")
            else:
                image_url = images[0].get("url")

            if not image_url:
                raise ProviderError(f"fal face_swap: no image URL in result: {result}")

            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(image_url)
                resp.raise_for_status()
                media_bytes = resp.content

            return GenerationOutput(
                media_bytes=media_bytes,
                cost_paise=self._cost_paise,
                provider_name="fal.ai/face-swap",
                media_type="image",
                model_id=_MODEL,
            )

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="FalFaceSwapAdapter")
