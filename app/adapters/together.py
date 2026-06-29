from __future__ import annotations

"""
Together AI image generation adapter.

- FLUX.1-schnell-Free : completely free with API key, same model as fal.ai
- FLUX.1-dev          : higher quality, paid (~$0.01/image)

Sign up at https://api.together.xyz — key is instant, no approval needed.
Set TOGETHER_API_KEY in Railway env vars.
"""

import logging

import httpx

from app.adapters.generation import GenerationOutput
from app.core.retry import ProviderError, with_timeout

logger = logging.getLogger(__name__)

_BASE = "https://api.together.xyz/v1"

_ASPECT_DIMS: dict[str, tuple[int, int]] = {
    "9:16": (768, 1344),
    "1:1":  (1024, 1024),
    "16:9": (1344, 768),
    "4:3":  (1024, 768),
    "3:4":  (768, 1024),
}


class TogetherImageAdapter:
    """
    Image generation via Together AI.

    Free model  : black-forest-labs/FLUX.1-schnell-Free  (cost_paise=0)
    Paid model  : black-forest-labs/FLUX.1-dev           (~$0.01/image)
    """

    def __init__(
        self,
        api_key: str,
        *,
        model_id: str = "black-forest-labs/FLUX.1-schnell-Free",
        cost_paise: int = 0,
        aspect_ratio: str = "9:16",
        timeout_seconds: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._model_id = model_id
        self._cost_paise = cost_paise
        self._width, self._height = _ASPECT_DIMS.get(aspect_ratio, (768, 1344))
        self._timeout_seconds = timeout_seconds

    async def generate(self, prompt: str) -> GenerationOutput:
        async def _run() -> GenerationOutput:
            logger.info("TogetherImageAdapter: model=%s", self._model_id)
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.post(
                    f"{_BASE}/images/generations",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._model_id,
                        "prompt": prompt,
                        "n": 1,
                        "width": self._width,
                        "height": self._height,
                        "response_format": "url",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            images = data.get("data") or []
            if not images:
                raise ProviderError(f"Together AI: no images in response: {data}")

            image_url: str = images[0].get("url") or images[0].get("b64_json", "")
            if not image_url:
                raise ProviderError(f"Together AI: no URL in image data: {images[0]}")

            async with httpx.AsyncClient(timeout=60.0) as client:
                img_resp = await client.get(image_url)
                img_resp.raise_for_status()
                media_bytes = img_resp.content

            logger.info("TogetherImageAdapter: done, %d bytes", len(media_bytes))
            return GenerationOutput(
                media_bytes=media_bytes,
                cost_paise=self._cost_paise,
                provider_name="together.ai",
                media_type="image",
                model_id=self._model_id,
            )

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="TogetherImageAdapter")
