from __future__ import annotations

"""
GenerationProvider Protocol and concrete adapters.

Adapters
--------
FakeGenerationAdapter  — deterministic, no network calls; used in tests
CompositeGenerationAdapter — cheap stock-asset compositing path (stub)
FalImageAdapter / FalVideoAdapter — real fal.ai calls (in app/adapters/fal.py)

Cost logging
------------
Every generate() call should be followed by log_generation_cost() so the
structured log line reaches your monitoring stack before the DB write.
The DB column generation_jobs.cost_paise is the authoritative record;
the log line is for real-time alerting and dashboards.

  WHERE to watch cost
  -------------------
  1. generation_jobs table  — cost_paise column, queryable with SQL
  2. Structured log line    — grep/query for "generation_cost" in your log
                              aggregator (Loki, CloudWatch, Datadog, etc.)
  3. Worker stdout          — INFO-level during development
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Structured log name — grep this to find every cost event
_COST_LOG_NAME = "generation_cost"

# 1 KB placeholder image (white PNG, 1×1 pixel) — used by stub adapters
_PLACEHOLDER_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass(frozen=True)
class GenerationOutput:
    media_bytes: bytes          # raw image or video bytes
    cost_paise: int             # provider cost; ALWAYS integer paise, never float
    provider_name: str          # e.g. "fal.ai", "composite-stub", "fake"
    media_type: str = "image"   # "image" | "video"
    model_id: str = ""          # provider model identifier for logging


@dataclass(frozen=True)
class CostEvent:
    order_id: int
    job_id: int
    provider_name: str
    model_id: str
    cost_paise: int
    media_type: str
    duration_ms: int


def log_generation_cost(event: CostEvent) -> None:
    """
    Emit a structured log line for every generation.

    The logger name is "generation_cost" — configure your log aggregator
    to ship lines from this logger to your cost dashboard.  Example query
    (Loki):  {job="weddingapp-worker"} |= "generation_cost"
    """
    cost_logger = logging.getLogger(_COST_LOG_NAME)
    cost_logger.info(
        "provider=%s model=%s media_type=%s cost_paise=%d duration_ms=%d "
        "order_id=%d job_id=%d",
        event.provider_name,
        event.model_id,
        event.media_type,
        event.cost_paise,
        event.duration_ms,
        event.order_id,
        event.job_id,
    )


@runtime_checkable
class GenerationProvider(Protocol):
    async def generate(self, prompt: str) -> GenerationOutput: ...


# ---------------------------------------------------------------------------
# Stub adapters (no external calls — kept for local dev and as fallback)
# ---------------------------------------------------------------------------


class CompositeGenerationAdapter:
    """
    Cheap path: composites stock assets rather than calling a paid model.
    Stub — real compositing logic wired in a future PR.
    """

    COST_PAISE = 100  # ₹1

    async def generate(self, prompt: str) -> GenerationOutput:
        logger.info("CompositeGenerationAdapter: stub generate for prompt=%r", prompt[:80])
        return GenerationOutput(
            media_bytes=_PLACEHOLDER_PNG,
            cost_paise=self.COST_PAISE,
            provider_name="composite-stub",
            media_type="image",
        )


class FullGenerationAdapter:
    """
    Premium path stub — replaced by FalImageAdapter / FalVideoAdapter in production.
    Kept so existing tests that reference this class continue to resolve.
    """

    COST_PAISE = 500  # ₹5

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key

    async def generate(self, prompt: str) -> GenerationOutput:
        logger.info("FullGenerationAdapter: stub generate for prompt=%r", prompt[:80])
        return GenerationOutput(
            media_bytes=_PLACEHOLDER_PNG,
            cost_paise=self.COST_PAISE,
            provider_name="full-gen-stub",
            media_type="image",
        )


class FakeGenerationAdapter:
    """Deterministic fake for tests — no network calls, no external state."""

    COST_PAISE = 200

    async def generate(self, prompt: str) -> GenerationOutput:
        return GenerationOutput(
            media_bytes=b"fake-image-bytes",
            cost_paise=self.COST_PAISE,
            provider_name="fake",
            media_type="image",
            model_id="fake",
        )


class FakeVideoGenerationAdapter:
    """Deterministic fake for video tests."""

    COST_PAISE = 400

    async def generate(self, prompt: str) -> GenerationOutput:
        return GenerationOutput(
            media_bytes=b"fake-video-bytes",
            cost_paise=self.COST_PAISE,
            provider_name="fake",
            media_type="video",
            model_id="fake-video",
        )
