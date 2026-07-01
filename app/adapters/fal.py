from __future__ import annotations

"""
fal.ai generation adapter — image and video paths.

Uses the official fal-client Python SDK (fal_client.submit / subscribe)
which handles queue submit + poll + result fetch internally and stays
in sync with fal.ai API changes automatically.
"""

import logging

import fal_client

from app.adapters.generation import GenerationOutput
from app.core.retry import ProviderError, with_timeout

logger = logging.getLogger(__name__)


def _set_fal_key(api_key: str) -> None:
    """Configure fal-client credentials."""
    import os
    os.environ["FAL_KEY"] = api_key


class FalImageAdapter:
    """
    Generates images via fal.ai.

    Default model : fal-ai/flux/schnell
    Standard cost : configured via gen_image_cost_paise (default 250 paise = ₹2.50)
    """

    def __init__(
        self,
        api_key: str,
        *,
        model_id: str = "fal-ai/flux/schnell",
        cost_paise: int = 250,
        image_size: str = "landscape_4_3",
        max_retries: int = 3,
        timeout_seconds: float = 300.0,
    ) -> None:
        _set_fal_key(api_key)
        self._model_id = model_id
        self._cost_paise = cost_paise
        self._image_size = image_size
        self._timeout_seconds = timeout_seconds

    async def generate(self, prompt: str) -> GenerationOutput:
        import httpx

        async def _run() -> GenerationOutput:
            logger.info("fal.ai submitting: model=%s", self._model_id)

            handler = await fal_client.submit_async(
                self._model_id,
                arguments={
                    "prompt": prompt,
                    "image_size": self._image_size,
                    "num_inference_steps": 4,
                    "num_images": 1,
                },
            )

            result = await handler.get()
            logger.info("fal.ai done: model=%s request_id=%s", self._model_id, handler.request_id)

            images = result.get("images") or []
            if not images:
                raise ProviderError("fal.ai image: no images in result")

            image_url: str = images[0]["url"]
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(image_url)
                resp.raise_for_status()
                media_bytes = resp.content

            return GenerationOutput(
                media_bytes=media_bytes,
                cost_paise=self._cost_paise,
                provider_name="fal.ai",
                media_type="image",
                model_id=self._model_id,
            )

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="FalImageAdapter")

    async def aclose(self) -> None:
        pass


class FalVideoAdapter:
    """
    Generates short videos via fal.ai.

    Default model : fal-ai/kling-video/v2.1/standard/image-to-video
    Standard cost : configured via gen_video_cost_paise (default 2500 paise = ₹25)

    Supports both text-to-video (generate) and image-to-video (generate_from_image).
    Kling and most modern fal.ai video models return {"video": {"url": "..."}}.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model_id: str = "fal-ai/kling-video/v2.1/standard/image-to-video",
        cost_paise: int = 2500,
        max_retries: int = 3,
        timeout_seconds: float = 600.0,
    ) -> None:
        _set_fal_key(api_key)
        self._model_id = model_id
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds

    async def _submit_and_download(self, arguments: dict) -> GenerationOutput:
        import httpx

        async def _run() -> GenerationOutput:
            logger.info("fal.ai submitting video: model=%s", self._model_id)
            handler = await fal_client.submit_async(self._model_id, arguments=arguments)
            result = await handler.get()
            logger.info("fal.ai video done: model=%s request_id=%s", self._model_id, handler.request_id)

            video = result.get("video") or {}
            video_url: str | None = video.get("url") if isinstance(video, dict) else None
            if not video_url:
                raise ProviderError("fal.ai video: no video url in result")

            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.get(video_url)
                resp.raise_for_status()
                media_bytes = resp.content

            return GenerationOutput(
                media_bytes=media_bytes,
                cost_paise=self._cost_paise,
                provider_name="fal.ai",
                media_type="video",
                model_id=self._model_id,
            )

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="FalVideoAdapter")

    async def generate(self, prompt: str) -> GenerationOutput:
        return await self._submit_and_download({"prompt": prompt})

    async def generate_from_image(
        self,
        prompt: str,
        image_url: str,
        *,
        duration: str = "5",
        aspect_ratio: str = "9:16",
    ) -> GenerationOutput:
        """Image-to-video: animate a still image with optional motion prompt."""
        return await self._submit_and_download({
            "prompt": prompt,
            "image_url": image_url,
            "duration": duration,
            "aspect_ratio": aspect_ratio,
        })

    async def aclose(self) -> None:
        pass
