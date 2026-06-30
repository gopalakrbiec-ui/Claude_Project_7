from __future__ import annotations

"""
fal.ai InstantID adapter — places the user's face into a template scene.

InstantID preserves face identity while applying the style/pose of a reference
image. It produces photorealistic, DSLR-quality portraits.

Model: fal-ai/instantid (primary)
Fallback: fal-ai/flux/dev/image-to-image (if instantid unavailable)

Inputs:
  - face_image_url  : presigned URL of the user's uploaded photo (from R2)
  - style_image_url : public URL of the template reference image
  - prompt          : scene description + user free-text
  - aspect_ratio    : "1:1" | "9:16" | "16:9" | "4:3" | "3:4"
"""

import logging

import fal_client

from app.adapters.generation import GenerationOutput
from app.core.retry import ProviderError, with_timeout

logger = logging.getLogger(__name__)

# Map aspect ratio strings to pixel dimensions (optimised for mobile display)
_ASPECT_SIZES: dict[str, tuple[int, int]] = {
    "1:1":  (1024, 1024),
    "9:16": (768, 1344),
    "16:9": (1344, 768),
    "4:3":  (1024, 768),
    "3:4":  (768, 1024),
}

_INSTANTID_MODEL = "fal-ai/instantid"
_FALLBACK_MODEL  = "fal-ai/flux/dev/image-to-image"


def _set_fal_key(api_key: str) -> None:
    import os
    os.environ["FAL_KEY"] = api_key


class InstantIDAdapter:
    """
    Generates a photorealistic portrait of the user placed in the template scene.

    When face_image_url is None (user skipped photo), falls back to pure
    text-to-image via FalImageAdapter so the template still produces output.
    """

    def __init__(
        self,
        api_key: str,
        *,
        cost_paise: int = 500,
        timeout_seconds: float = 300.0,
        max_retries: int = 3,
    ) -> None:
        _set_fal_key(api_key)
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds

    async def generate_with_face(
        self,
        *,
        prompt: str,
        face_image_url: str,
        style_image_url: str | None = None,
        aspect_ratio: str = "9:16",
    ) -> GenerationOutput:
        """User photo provided — use InstantID for face-accurate output."""
        width, height = _ASPECT_SIZES.get(aspect_ratio, (768, 1344))

        async def _run() -> GenerationOutput:
            logger.info("InstantID submit: aspect=%s face=%s", aspect_ratio, face_image_url[:60])

            arguments: dict = {
                "prompt": prompt,
                "face_image_url": face_image_url,
                "image_size": {"width": width, "height": height},
                "num_inference_steps": 30,
                "guidance_scale": 5.0,
                # Disabled: we run our own moderation gate before this call.
                # The fal.ai checker blocks real faces and returns empty images.
                "enable_safety_checker": False,
            }
            if style_image_url:
                arguments["pose_image_url"] = style_image_url

            handler = await fal_client.submit_async(_INSTANTID_MODEL, arguments=arguments)
            result = await handler.get()
            logger.info("InstantID raw result keys: %s", list(result.keys()) if isinstance(result, dict) else result)

            # fal.ai returns {"images": [...]} — handle both list and single-image shapes
            images = result.get("images") or []
            if not images and result.get("image"):
                images = [result["image"]]
            if not images:
                logger.error("InstantID: unexpected result shape: %s", result)
                raise ProviderError(f"InstantID: no images in result (keys={list(result.keys()) if isinstance(result, dict) else '?'})")

            import httpx
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(images[0]["url"])
                resp.raise_for_status()
                media_bytes = resp.content

            return GenerationOutput(
                media_bytes=media_bytes,
                cost_paise=self._cost_paise,
                provider_name="fal.ai/instantid",
                media_type="image",
                model_id=_INSTANTID_MODEL,
            )

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="InstantIDAdapter")

    async def generate(self, prompt: str) -> GenerationOutput:
        """Protocol-compatible fallback — text-only, no face reference."""
        from app.adapters.fal import FalImageAdapter
        import os
        adapter = FalImageAdapter(
            api_key=os.environ.get("FAL_KEY", ""),
            cost_paise=self._cost_paise,
            timeout_seconds=self._timeout_seconds,
        )
        return await adapter.generate(prompt)

    async def aclose(self) -> None:
        pass
