from __future__ import annotations

"""
Fireworks AI image generation adapter.

Fastest inference available (~2-4s per image). OpenAI-compatible API.
Free $1 credit on signup, then ~$0.002/image for Flux Schnell.

Sign up at https://fireworks.ai — key is instant, no approval.
Set FIREWORKS_API_KEY in Railway env vars.
"""

import logging

import httpx

from app.adapters.generation import GenerationOutput
from app.core.retry import ProviderError, with_timeout

logger = logging.getLogger(__name__)

_BASE = "https://api.fireworks.ai/inference/v1"

_ASPECT_DIMS: dict[str, tuple[int, int]] = {
    "9:16": (768, 1344),
    "1:1":  (1024, 1024),
    "16:9": (1344, 768),
    "4:3":  (1024, 768),
    "3:4":  (768, 1024),
}


class FireworksImageAdapter:
    """
    Image generation via Fireworks AI.

    Default model : accounts/fireworks/models/flux-1-schnell-fp8
    Cost          : ~$0.002/image (~0.17 paise) — charged as configured cost_paise
    """

    def __init__(
        self,
        api_key: str,
        *,
        model_id: str = "accounts/fireworks/models/flux-1-schnell-fp8",
        cost_paise: int = 20,
        aspect_ratio: str = "9:16",
        timeout_seconds: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._model_id = model_id
        self._cost_paise = cost_paise
        self._width, self._height = _ASPECT_DIMS.get(aspect_ratio, (768, 1344))
        self._timeout_seconds = timeout_seconds

    async def generate(self, prompt: str) -> GenerationOutput:
        async def _run() -> GenerationOutput:
            logger.info("FireworksImageAdapter: model=%s", self._model_id)
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.post(
                    f"{_BASE}/image_generation/{self._model_id}",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "prompt": prompt,
                        "width": self._width,
                        "height": self._height,
                        "num_inference_steps": 4,
                        "guidance_scale": 0,
                        "output_image_format": "PNG",
                    },
                )
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")
                if "image" in content_type:
                    # Fireworks returns raw image bytes directly
                    media_bytes = resp.content
                else:
                    data = resp.json()
                    images = data.get("images") or data.get("data") or []
                    if not images:
                        raise ProviderError(f"Fireworks: no images in response: {data}")
                    image_url = images[0].get("url")
                    if not image_url:
                        raise ProviderError(f"Fireworks: no URL in image data: {images[0]}")
                    img_resp = await client.get(image_url)
                    img_resp.raise_for_status()
                    media_bytes = img_resp.content

            logger.info("FireworksImageAdapter: done, %d bytes", len(media_bytes))
            return GenerationOutput(
                media_bytes=media_bytes,
                cost_paise=self._cost_paise,
                provider_name="fireworks.ai",
                media_type="image",
                model_id=self._model_id,
            )

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="FireworksImageAdapter")
