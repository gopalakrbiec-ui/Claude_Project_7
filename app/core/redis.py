from __future__ import annotations

from functools import lru_cache

import redis.asyncio as aioredis

from app.core.config import get_settings


def _make_redis() -> aioredis.Redis:
    settings = get_settings()
    return aioredis.from_url(
        str(settings.redis_url),
        encoding="utf-8",
        decode_responses=True,
    )


_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = _make_redis()
    return _redis
