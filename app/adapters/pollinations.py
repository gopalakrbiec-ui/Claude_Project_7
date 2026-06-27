from __future__ import annotations

"""
Pollinations.ai generation adapter — free, no API key required.

Image endpoint: GET https://image.pollinations.ai/prompt/{encoded_prompt}
Returns image bytes directly (JPEG). No account, no billing, no rate limits
(generous fair-use). Suitable for development and low-volume production.

Docs: https://pollinations.ai
"""

import logging
import urllib.parse

import httpx

from app.adapters.generation import GenerationOutput
from app.core.retry import ProviderError, with_timeout

logger = logging.getLogger(__name__)

_BASE_URL = "https://image.pollinations.ai/prompt"


class PollinationsImageAdapter:
    """
    Generates images via Pollinations.ai (free, no API key).

    Default model: flux (FLUX.1 via Pollinations)
    Cost: 0 paise (free service) — still logged for observability
    """

    def __init__(
        self,
        *,
        model: str = "flux",
        width: int = 768,
        height: int = 512,
        cost_paise: int = 0,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._model = model
        self._width = width
        self._height = height
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds

    async def generate(self, prompt: str) -> GenerationOutput:
        async def _run() -> GenerationOutput:
            encoded = urllib.parse.quote(prompt, safe="")
            url = (
                f"{_BASE_URL}/{encoded}"
                f"?width={self._width}&height={self._height}"
                f"&model={self._model}&nologo=true&enhance=false"
            )
            logger.info("Pollinations request: model=%s width=%d height=%d", self._model, self._width, self._height)

            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.get(url, follow_redirects=True)
                if resp.status_code >= 400:
                    raise ProviderError(f"Pollinations error {resp.status_code}: {resp.text[:200]}")
                media_bytes = resp.content

            if not media_bytes:
                raise ProviderError("Pollinations returned empty response")

            logger.info("Pollinations done: %d bytes received", len(media_bytes))
            return GenerationOutput(
                media_bytes=media_bytes,
                cost_paise=self._cost_paise,
                provider_name="pollinations.ai",
                media_type="image",
                model_id=f"pollinations/{self._model}",
            )

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="PollinationsImageAdapter")

    async def aclose(self) -> None:
        pass
