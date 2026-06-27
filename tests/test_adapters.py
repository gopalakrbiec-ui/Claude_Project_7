"""
Tests for fal.ai generation adapters, retry utilities, and cost logging.

HTTP layer is mocked with httpx.MockTransport so no real network calls are made.
Anthropic layer is mocked with unittest.mock so no real API calls are made.
"""
from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.adapters.fal import FalImageAdapter, FalVideoAdapter
from app.adapters.generation import (
    CostEvent,
    FakeGenerationAdapter,
    GenerationOutput,
    log_generation_cost,
)
from app.adapters.moderation import ClaudeModerationAdapter, FakeModerationAdapter
from app.adapters.claude import ClaudePromptAdapter, FakeClaudeAdapter
from app.core.retry import (
    HardTimeoutError,
    ProviderError,
    RetryExhaustedError,
    retrying,
    with_timeout,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_transport(responses: list[httpx.Response]) -> httpx.AsyncClient:
    """
    Build a real httpx.AsyncClient whose transport replays *responses* in order.
    """
    idx = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal idx
        resp = responses[idx]
        idx = min(idx + 1, len(responses) - 1)
        return resp

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


def _json_resp(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data)


def _err_resp(status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": "provider error"})


# ---------------------------------------------------------------------------
# app.core.retry tests
# ---------------------------------------------------------------------------


async def test_retrying_succeeds_first_attempt():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        return 42

    result = await retrying(fn, max_attempts=3, label="test")
    assert result == 42
    assert calls == 1


async def test_retrying_retries_on_transport_error():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.TransportError("connection reset")
        return "ok"

    result = await retrying(fn, max_attempts=3, base_delay=0, label="test")
    assert result == "ok"
    assert calls == 3


async def test_retrying_exhausted_raises():
    async def fn():
        raise httpx.TransportError("always fails")

    with pytest.raises(RetryExhaustedError):
        await retrying(fn, max_attempts=2, base_delay=0, label="test")


async def test_retrying_does_not_retry_4xx():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        resp = httpx.Response(422, json={"detail": "bad request"})
        raise httpx.HTTPStatusError("422", request=MagicMock(), response=resp)

    with pytest.raises(ProviderError) as exc_info:
        await retrying(fn, max_attempts=3, base_delay=0, label="test")

    assert calls == 1  # not retried
    assert exc_info.value.status_code == 422


async def test_retrying_retries_5xx():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        if calls < 3:
            resp = httpx.Response(503, json={"error": "overloaded"})
            raise httpx.HTTPStatusError("503", request=MagicMock(), response=resp)
        return "recovered"

    result = await retrying(fn, max_attempts=3, base_delay=0, label="test")
    assert result == "recovered"
    assert calls == 3


async def test_with_timeout_completes():
    async def fast():
        return "done"

    result = await with_timeout(fast(), seconds=5.0, label="test")
    assert result == "done"


async def test_with_timeout_raises_on_expiry():
    async def slow():
        await asyncio.sleep(10)
        return "never"

    with pytest.raises(HardTimeoutError):
        await with_timeout(slow(), seconds=0.01, label="slow_op")


async def test_hard_timeout_not_retried():
    """HardTimeoutError must propagate immediately through retrying()."""
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise HardTimeoutError("timed out")

    with pytest.raises(HardTimeoutError):
        await retrying(fn, max_attempts=5, base_delay=0, label="test")

    assert calls == 1  # propagated immediately, not retried


# ---------------------------------------------------------------------------
# FalImageAdapter tests (httpx mocked)
# ---------------------------------------------------------------------------


async def test_fal_image_adapter_happy_path():
    """submit → get result → download image bytes (via fal_client SDK mock)."""
    image_bytes = b"\x89PNG fake image data"

    mock_handler = AsyncMock()
    mock_handler.request_id = "req-abc123"
    mock_handler.get = AsyncMock(return_value={
        "images": [{"url": "https://cdn.fal.ai/img.png", "width": 512, "height": 512}]
    })

    with patch("fal_client.submit_async", return_value=mock_handler), \
         patch("httpx.AsyncClient") as mock_http_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.content = image_bytes
        mock_resp.raise_for_status = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http_cls.return_value = mock_http

        adapter = FalImageAdapter(api_key="test-key", cost_paise=250, model_id="fal-ai/flux/dev")
        output = await adapter.generate("A beautiful floral wedding stage")

    assert output.media_bytes == image_bytes
    assert output.cost_paise == 250
    assert output.provider_name == "fal.ai"
    assert output.media_type == "image"
    assert output.model_id == "fal-ai/flux/dev"


async def test_fal_image_adapter_timeout():
    """Hard timeout wraps the entire generate() call."""
    async def slow_submit(*args, **kwargs):
        await asyncio.sleep(10)

    with patch("fal_client.submit_async", side_effect=slow_submit):
        adapter = FalImageAdapter(api_key="test-key", timeout_seconds=0.05)
        with pytest.raises(HardTimeoutError):
            await adapter.generate("Wedding stage")


async def test_fal_image_raises_when_no_images():
    """fal.ai returns empty images list → ProviderError."""
    mock_handler = AsyncMock()
    mock_handler.request_id = "req-fail"
    mock_handler.get = AsyncMock(return_value={"images": []})

    with patch("fal_client.submit_async", return_value=mock_handler):
        adapter = FalImageAdapter(api_key="test-key", max_retries=1)
        with pytest.raises(ProviderError, match="no images"):
            await adapter.generate("Wedding stage")


# ---------------------------------------------------------------------------
# FalVideoAdapter tests
# ---------------------------------------------------------------------------


async def test_fal_video_adapter_happy_path():
    video_bytes = b"fake mp4 data"

    mock_handler = AsyncMock()
    mock_handler.request_id = "req-vid-1"
    mock_handler.get = AsyncMock(return_value={
        "video": {"url": "https://cdn.fal.ai/vid.mp4"}
    })

    with patch("fal_client.submit_async", return_value=mock_handler), \
         patch("httpx.AsyncClient") as mock_http_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.content = video_bytes
        mock_resp.raise_for_status = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http_cls.return_value = mock_http

        adapter = FalVideoAdapter(api_key="test-key", cost_paise=800, model_id="fal-ai/cogvideox-5b")
        output = await adapter.generate("Bride and groom in a floral garden")

    assert output.media_bytes == video_bytes
    assert output.cost_paise == 800
    assert output.media_type == "video"
    assert output.model_id == "fal-ai/cogvideox-5b"


async def test_fal_video_raises_when_no_url():
    mock_handler = AsyncMock()
    mock_handler.request_id = "req-vid-2"
    mock_handler.get = AsyncMock(return_value={"video": {}})  # missing url

    with patch("fal_client.submit_async", return_value=mock_handler):
        adapter = FalVideoAdapter(api_key="test-key", max_retries=1)
        with pytest.raises(ProviderError, match="no video url"):
            await adapter.generate("Some prompt")


# ---------------------------------------------------------------------------
# Cost logging hook tests
# ---------------------------------------------------------------------------


def test_log_generation_cost_emits_structured_line(caplog):
    event = CostEvent(
        order_id=42,
        job_id=7,
        provider_name="fal.ai",
        model_id="fal-ai/flux/dev",
        cost_paise=250,
        media_type="image",
        duration_ms=1234,
    )

    with caplog.at_level(logging.INFO, logger="generation_cost"):
        log_generation_cost(event)

    assert len(caplog.records) == 1
    rec = caplog.records[0]
    assert rec.name == "generation_cost"
    assert "cost_paise=250" in rec.message
    assert "order_id=42" in rec.message
    assert "fal.ai" in rec.message
    assert "duration_ms=1234" in rec.message


def test_log_generation_cost_video(caplog):
    event = CostEvent(
        order_id=99,
        job_id=5,
        provider_name="fal.ai",
        model_id="fal-ai/cogvideox-5b",
        cost_paise=800,
        media_type="video",
        duration_ms=45000,
    )

    with caplog.at_level(logging.INFO, logger="generation_cost"):
        log_generation_cost(event)

    assert "media_type=video" in caplog.records[0].message
    assert "cost_paise=800" in caplog.records[0].message


# ---------------------------------------------------------------------------
# ClaudeModerationAdapter tests (Anthropic SDK mocked)
# ---------------------------------------------------------------------------


async def test_claude_moderation_allows_safe_content():
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=json.dumps({"allowed": True, "reason": ""}))]

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        adapter = ClaudeModerationAdapter(api_key="test-key")
        result = await adapter.moderate("Beautiful floral wedding theme, bride and groom")

    assert result.allowed is True
    assert result.reason == ""


async def test_claude_moderation_blocks_celebrity():
    mock_response = MagicMock()
    mock_response.content = [
        MagicMock(
            text=json.dumps({"allowed": False, "reason": "Named real celebrity detected"})
        )
    ]

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        adapter = ClaudeModerationAdapter(api_key="test-key")
        result = await adapter.moderate("Virat Kohli wedding poster")

    assert result.allowed is False
    assert "celebrity" in result.reason.lower()


async def test_claude_moderation_blocks_on_timeout():
    """Moderation timeout returns allowed=False (conservative default)."""
    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = AsyncMock()

        async def slow_create(*args, **kwargs):
            await asyncio.sleep(10)

        mock_client.messages.create = slow_create
        mock_cls.return_value = mock_client

        adapter = ClaudeModerationAdapter(api_key="test-key", timeout_seconds=0.05)
        result = await adapter.moderate("Some content")

    assert result.allowed is False
    assert "timeout" in result.reason.lower()


async def test_claude_moderation_retries_on_connection_error():
    """APIConnectionError triggers retry; succeeds on third attempt."""
    from anthropic import APIConnectionError

    call_count = 0
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=json.dumps({"allowed": True, "reason": ""}))]

    async def flaky_create(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise APIConnectionError.__new__(APIConnectionError)
        return mock_response

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = flaky_create
        mock_cls.return_value = mock_client

        adapter = ClaudeModerationAdapter(api_key="test-key", max_retries=3)
        # patch sleep so the test doesn't actually wait
        with patch("app.core.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await adapter.moderate("Wedding theme")

    assert result.allowed is True
    assert call_count == 3


async def test_claude_moderation_handles_unparseable_response():
    """Unparseable JSON response blocks by default (conservative)."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="sorry I cannot help with that")]

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        adapter = ClaudeModerationAdapter(api_key="test-key")
        result = await adapter.moderate("Some content")

    assert result.allowed is False
    assert "parse error" in result.reason.lower()


# ---------------------------------------------------------------------------
# ClaudePromptAdapter tests
# ---------------------------------------------------------------------------


async def test_claude_prompt_adapter_parses_response():
    expected = {
        "generation_prompt": "Elegant floral mandap, warm golden lighting, traditional attire",
        "caption_text": "राज और प्रिया के विवाह में आपका स्वागत है",
    }
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=json.dumps(expected))]

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        adapter = ClaudePromptAdapter(api_key="test-key")
        result = await adapter.build_prompt(
            {"names": "Raj & Priya", "theme": "floral"}, language="hi"
        )

    assert result.generation_prompt == expected["generation_prompt"]
    assert result.caption_text == expected["caption_text"]


async def test_claude_prompt_adapter_handles_code_fence():
    """Strips markdown code fences before JSON parse."""
    inner = {"generation_prompt": "Test prompt", "caption_text": "Test caption"}
    raw = f"```json\n{json.dumps(inner)}\n```"
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=raw)]

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        adapter = ClaudePromptAdapter(api_key="test-key")
        result = await adapter.build_prompt({}, language="hi")

    assert result.generation_prompt == "Test prompt"


async def test_claude_prompt_adapter_falls_back_on_parse_error():
    """Unparseable response uses raw text as generation_prompt."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="not json at all")]

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        adapter = ClaudePromptAdapter(api_key="test-key")
        result = await adapter.build_prompt({}, language="hi")

    assert result.generation_prompt == "not json at all"
    assert result.caption_text == ""


async def test_claude_prompt_adapter_timeout():
    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = AsyncMock()

        async def slow_create(*args, **kwargs):
            await asyncio.sleep(10)

        mock_client.messages.create = slow_create
        mock_cls.return_value = mock_client

        adapter = ClaudePromptAdapter(api_key="test-key", timeout_seconds=0.05)
        with pytest.raises(HardTimeoutError):
            await adapter.build_prompt({}, language="hi")
