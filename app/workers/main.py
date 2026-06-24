from __future__ import annotations

from arq.connections import RedisSettings

from app.core.config import get_settings
from app.workers.jobs import generate_content


def _redis_settings() -> RedisSettings:
    settings = get_settings()
    url = str(settings.redis_url)
    # arq expects host/port/db separately; parse from DSN.
    # redis://host:port/db
    parts = url.replace("redis://", "").split("/")
    host_port = parts[0].split(":")
    host = host_port[0]
    port = int(host_port[1]) if len(host_port) > 1 else 6379
    db = int(parts[1]) if len(parts) > 1 else 0
    return RedisSettings(host=host, port=port, database=db)


# ---------------------------------------------------------------------------
# Job functions go here as the project grows.
# ---------------------------------------------------------------------------


class WorkerSettings:
    """Arq worker configuration."""

    redis_settings = _redis_settings()
    max_jobs = get_settings().arq_max_jobs
    functions = [generate_content]
    cron_jobs: list = []
