from __future__ import annotations

"""
fal.ai generation adapter — image and video paths.

Why fal.ai
----------
All fal.ai models share one queue API pattern regardless of modality:

  POST  queue.fal.run/{model_id}            → enqueue, get request_id
  GET   queue.fal.run/{model_id}/requests/{id}/status  → poll
  GET   queue.fal.run/{model_id}/requests/{id}         → fetch result

This lets FalImageAdapter and FalVideoAdapter share a single _FalClient
while keeping the GenerationProvider interface identical for both callers.
Adding a new model (LoRA fine-tune, upscaler, etc.) is a two-line subclass.

Cost accounting
---------------
fal.ai does not return per-request cost in the result payload — they bill
via invoice.  We store a *configured standard cost* per model in settings
(gen_image_cost_paise, gen_video_cost_paise).  This is good enough for the
debit ledger; reconciliation against the fal.ai invoice catches drift.

The generation_jobs.cost_paise column is the authoritative record.
The structured cost log line (emitted by log_generation_cost) feeds
real-time alerting before the invoice arrives.

Retry / timeout
---------------
Each HTTP call uses httpx's own connect+read timeout (_HTTP_TIMEOUT).
The full submit+poll cycle is wrapped in a hard wall-clock timeout
supplied by the caller (via app.core.retry.with_timeout).  Retries use
app.core.retry.retrying() with exponential back-off + full jitter.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

from app.adapters.generation import GenerationOutput
from app.core.retry import ProviderError, retrying

logger = logging.getLogger(__name__)

# Per-call connect + read timeout for individual HTTP requests.
# The hard wall-clock budget is applied by the caller via with_timeout().
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0)

_FAL_QUEUE_BASE = "https://queue.fal.run"


@dataclass
class _PollConfig:
    """How long to wait between status polls."""

    initial_delay: float = 2.0   # seconds before first poll
    backoff_factor: float = 1.5  # multiply delay each round
    max_delay: float = 15.0      # cap


class _FalClient:
    """
    Thin async HTTP wrapper around the fal.ai queue API.
    Not instantiated directly — use FalImageAdapter or FalVideoAdapter.
    """

    def __init__(self, api_key: str, *, max_retries: int = 3) -> None:
        self._headers = {
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
        }
        self._max_retries = max_retries
        # Single client, shared across all requests from this adapter instance.
        # keep_alive=True so the TCP connection is reused for submit+poll.
        self._http = httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            headers=self._headers,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _submit(self, model_id: str, payload: dict) -> str:
        """Enqueue a job and return the request_id."""
        url = f"{_FAL_QUEUE_BASE}/{model_id}"

        async def _call() -> str:
            resp = await self._http.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            request_id: str = data["request_id"]
            return request_id

        return await retrying(_call, max_attempts=self._max_retries, label=f"fal.submit({model_id})")

    async def _poll_until_done(self, model_id: str, request_id: str) -> None:
        """Block until the fal.ai job reaches COMPLETED or FAILED."""
        status_url = f"{_FAL_QUEUE_BASE}/{model_id}/requests/{request_id}/status"
        cfg = _PollConfig()
        delay = cfg.initial_delay

        while True:
            async def _check() -> str:
                resp = await self._http.get(status_url)
                resp.raise_for_status()
                return resp.json().get("status", "UNKNOWN")

            status = await retrying(
                _check, max_attempts=self._max_retries, label=f"fal.poll({model_id})"
            )

            if status == "COMPLETED":
                return
            if status in ("FAILED", "CANCELLED"):
                raise ProviderError(f"fal.ai job {request_id} ended with status={status}")

            await asyncio.sleep(delay)
            delay = min(delay * cfg.backoff_factor, cfg.max_delay)

    async def _fetch_result(self, model_id: str, request_id: str) -> dict:
        """Fetch the completed result payload."""
        result_url = f"{_FAL_QUEUE_BASE}/{model_id}/requests/{request_id}"

        async def _call() -> dict:
            resp = await self._http.get(result_url)
            resp.raise_for_status()
            return resp.json()

        return await retrying(_call, max_attempts=self._max_retries, label=f"fal.result({model_id})")

    async def _download(self, url: str) -> bytes:
        """Download output bytes from a CDN URL."""
        async def _call() -> bytes:
            resp = await self._http.get(url)
            resp.raise_for_status()
            return resp.content

        return await retrying(_call, max_attempts=self._max_retries, label="fal.download")

    async def run(self, model_id: str, payload: dict) -> dict:
        """Submit → poll → fetch result. Returns the raw result dict."""
        t0 = time.monotonic()
        request_id = await self._submit(model_id, payload)
        logger.info("fal.ai job submitted: model=%s request_id=%s", model_id, request_id)

        await self._poll_until_done(model_id, request_id)

        result = await self._fetch_result(model_id, request_id)
        elapsed = time.monotonic() - t0
        logger.info(
            "fal.ai job done: model=%s request_id=%s elapsed=%.1fs",
            model_id,
            request_id,
            elapsed,
        )
        return result


class FalImageAdapter:
    """
    Generates images via fal.ai using the FLUX.1[dev] model by default.

    Default model : fal-ai/flux/dev
    Standard cost : configured via gen_image_cost_paise (default 250 paise = ₹2.50)

    The model can be overridden to fal-ai/stable-diffusion-xl or any other
    fal.ai image model without changing the adapter interface.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model_id: str = "fal-ai/flux/dev",
        cost_paise: int = 250,
        image_size: str = "landscape_4_3",
        max_retries: int = 3,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._client = _FalClient(api_key, max_retries=max_retries)
        self._model_id = model_id
        self._cost_paise = cost_paise
        self._image_size = image_size
        self._timeout_seconds = timeout_seconds

    async def generate(self, prompt: str) -> GenerationOutput:
        from app.core.retry import with_timeout

        payload = {
            "prompt": prompt,
            "image_size": self._image_size,
            "num_inference_steps": 28,
            "num_images": 1,
            "enable_safety_checker": True,
        }

        async def _run() -> GenerationOutput:
            result = await self._client.run(self._model_id, payload)
            images = result.get("images") or []
            if not images:
                raise ProviderError("fal.ai image: no images in result")
            image_url: str = images[0]["url"]
            media_bytes = await self._client._download(image_url)
            return GenerationOutput(
                media_bytes=media_bytes,
                cost_paise=self._cost_paise,
                provider_name="fal.ai",
                media_type="image",
                model_id=self._model_id,
            )

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="FalImageAdapter")

    async def aclose(self) -> None:
        await self._client.aclose()


class FalVideoAdapter:
    """
    Generates short videos via fal.ai using CogVideoX-5b by default.

    Default model : fal-ai/cogvideox-5b
    Standard cost : configured via gen_video_cost_paise (default 800 paise = ₹8)

    The model can be overridden to fal-ai/kling-video or any other
    fal.ai video model.  Output is always returned as raw bytes (mp4).
    """

    def __init__(
        self,
        api_key: str,
        *,
        model_id: str = "fal-ai/cogvideox-5b",
        cost_paise: int = 800,
        max_retries: int = 3,
        timeout_seconds: float = 600.0,
    ) -> None:
        self._client = _FalClient(api_key, max_retries=max_retries)
        self._model_id = model_id
        self._cost_paise = cost_paise
        self._timeout_seconds = timeout_seconds

    async def generate(self, prompt: str) -> GenerationOutput:
        from app.core.retry import with_timeout

        payload = {"prompt": prompt}

        async def _run() -> GenerationOutput:
            result = await self._client.run(self._model_id, payload)
            video = result.get("video") or {}
            video_url: str | None = video.get("url") if isinstance(video, dict) else None
            if not video_url:
                raise ProviderError("fal.ai video: no video url in result")
            media_bytes = await self._client._download(video_url)
            return GenerationOutput(
                media_bytes=media_bytes,
                cost_paise=self._cost_paise,
                provider_name="fal.ai",
                media_type="video",
                model_id=self._model_id,
            )

        return await with_timeout(_run(), seconds=self._timeout_seconds, label="FalVideoAdapter")

    async def aclose(self) -> None:
        await self._client.aclose()
