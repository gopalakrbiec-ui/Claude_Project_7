from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# 1 KB placeholder image (white PNG, 1x1 pixel)
_PLACEHOLDER_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass(frozen=True)
class GenerationOutput:
    image_bytes: bytes
    cost_paise: int  # provider cost; always integer paise, never float
    provider_name: str


@runtime_checkable
class GenerationProvider(Protocol):
    async def generate(self, prompt: str) -> GenerationOutput: ...


class CompositeGenerationAdapter:
    """
    Cheap path: composites stock assets rather than calling a paid model.
    Cost is very low (stock asset licensing, no model inference).
    Stub implementation — real compositing logic wired in a future PR.
    """

    COST_PAISE = 100  # ₹1

    async def generate(self, prompt: str) -> GenerationOutput:
        logger.info("CompositeGenerationAdapter: generating for prompt=%r (stub)", prompt[:80])
        return GenerationOutput(
            image_bytes=_PLACEHOLDER_PNG,
            cost_paise=self.COST_PAISE,
            provider_name="composite-stub",
        )


class FullGenerationAdapter:
    """
    Premium path: calls an external AI image/video generation API.
    Stub implementation — real provider SDK wired in a future PR.
    """

    COST_PAISE = 500  # ₹5

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def generate(self, prompt: str) -> GenerationOutput:
        logger.info("FullGenerationAdapter: generating for prompt=%r (stub)", prompt[:80])
        # TODO: replace with real provider SDK call (e.g. Stability AI, Runway)
        return GenerationOutput(
            image_bytes=_PLACEHOLDER_PNG,
            cost_paise=self.COST_PAISE,
            provider_name="full-gen-stub",
        )


class FakeGenerationAdapter:
    """Deterministic fake for tests — no external calls."""

    COST_PAISE = 200

    async def generate(self, prompt: str) -> GenerationOutput:
        return GenerationOutput(
            image_bytes=b"fake-image-bytes",
            cost_paise=self.COST_PAISE,
            provider_name="fake",
        )
