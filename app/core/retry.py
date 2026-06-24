from __future__ import annotations

"""
Retry and timeout utilities used by all external-API adapters.

Design choices
--------------
* Exponential back-off with *full jitter* (not decorrelated) so a burst of
  worker retries doesn't re-synchronise and hammer the provider together.
  Formula: sleep = random(0, min(cap, base * 2**attempt))

* We distinguish *retryable* errors (transient network / 5xx) from
  *terminal* errors (4xx, auth failure, hard timeout).  Terminal errors
  propagate immediately so the caller can mark the job failed rather than
  wasting time on doomed retries.

* The hard timeout wraps the ENTIRE retried call as a wall-clock budget.
  Each individual HTTP call uses a separate per-request connect/read timeout
  configured on the httpx client.  This means: even if every HTTP call
  finishes quickly, the budget caps the total number of retry rounds.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryExhaustedError(Exception):
    """Raised when all retry attempts fail."""


class HardTimeoutError(Exception):
    """Raised when the wall-clock budget for a call is exceeded."""


class ProviderError(Exception):
    """Non-retryable error from an external provider (4xx, bad response)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _is_retryable_http(exc: httpx.HTTPStatusError) -> bool:
    """Only retry server-side transient failures."""
    return exc.response.status_code >= 500


async def retrying(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    cap_delay: float = 30.0,
    label: str = "call",
) -> T:
    """
    Call *fn()* up to *max_attempts* times, backing off on transient errors.

    Retried exceptions
    ------------------
    * httpx.TransportError  — DNS / TCP failures
    * httpx.TimeoutException — per-request connect / read timeout
    * httpx.HTTPStatusError with status >= 500 — provider overloaded

    Non-retried exceptions propagate immediately:
    * httpx.HTTPStatusError with status < 500
    * ProviderError
    * HardTimeoutError (would mask asyncio.wait_for cancellation)
    * Everything else (programming errors, etc.)
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except HardTimeoutError:
            raise  # never mask a hard timeout
        except ProviderError:
            raise  # non-retryable provider error
        except httpx.HTTPStatusError as exc:
            if not _is_retryable_http(exc):
                raise ProviderError(
                    f"{label}: HTTP {exc.response.status_code}",
                    status_code=exc.response.status_code,
                ) from exc
            last_exc = exc
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
        except Exception:
            raise  # unexpected — propagate immediately

        if attempt < max_attempts - 1:
            delay = random.uniform(0, min(cap_delay, base_delay * (2**attempt)))
            logger.warning(
                "%s: attempt %d/%d failed (%s), retrying in %.1fs",
                label,
                attempt + 1,
                max_attempts,
                type(last_exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)

    raise RetryExhaustedError(
        f"{label}: all {max_attempts} attempts failed"
    ) from last_exc


async def with_timeout(coro: Awaitable[T], *, seconds: float, label: str = "call") -> T:
    """
    Hard wall-clock timeout around *coro*.  Raises HardTimeoutError on expiry.

    Use this to wrap the *entire* generate() call (submit + poll loop), not
    individual HTTP calls.  Individual HTTP calls should use httpx's own
    connect/read timeouts (set on the client).
    """
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError as exc:
        raise HardTimeoutError(f"{label} exceeded {seconds:.0f}s wall-clock budget") from exc


def retryable_anthropic(exc: Exception) -> bool:
    """Return True if an Anthropic SDK exception should be retried."""
    try:
        from anthropic import APIConnectionError, APIStatusError

        if isinstance(exc, APIConnectionError):
            return True
        if isinstance(exc, APIStatusError):
            # 529 = overloaded; 500/502/503/504 = transient
            return exc.status_code in (500, 502, 503, 504, 529)
    except ImportError:
        pass
    return False


async def retrying_anthropic(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    cap_delay: float = 30.0,
    label: str = "claude",
) -> T:
    """Like retrying() but for the Anthropic SDK exception hierarchy."""
    try:
        from anthropic import APIConnectionError, APIStatusError
    except ImportError:
        return await fn()

    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except HardTimeoutError:
            raise
        except (APIConnectionError, APIStatusError) as exc:
            if isinstance(exc, APIStatusError) and not retryable_anthropic(exc):
                raise  # 4xx auth / bad request — not retryable
            last_exc = exc
        except Exception:
            raise

        if attempt < max_attempts - 1:
            delay = random.uniform(0, min(cap_delay, base_delay * (2**attempt)))
            logger.warning(
                "%s: attempt %d/%d failed (%s), retrying in %.1fs",
                label,
                attempt + 1,
                max_attempts,
                type(last_exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)

    raise RetryExhaustedError(
        f"{label}: all {max_attempts} attempts failed"
    ) from last_exc
