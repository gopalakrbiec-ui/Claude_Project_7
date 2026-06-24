from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class JobQueue(Protocol):
    """Adapter interface for the async job queue (Arq in production)."""

    async def enqueue(self, function: str, **kwargs) -> None:
        """Enqueue a named job with arbitrary keyword arguments."""
        ...


class ArqJobQueue:
    """
    Production implementation backed by Arq + Redis.

    A single ArqJobQueue instance can be shared across requests because
    arq.ArqRedis connection pools are thread/task-safe.
    """

    def __init__(self, redis_pool) -> None:  # arq.ArqRedis
        self._pool = redis_pool

    async def enqueue(self, function: str, **kwargs) -> None:
        await self._pool.enqueue_job(function, **kwargs)


class LoggingJobQueue:
    """
    Dev/test stub — logs the enqueue call instead of touching Redis.
    Useful for integration tests that don't need a real worker to run.
    """

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue(self, function: str, **kwargs) -> None:
        self.enqueued.append((function, kwargs))
        logger.info("DEV QUEUE  function=%s kwargs=%s", function, kwargs)
