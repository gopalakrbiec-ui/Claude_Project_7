from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    env: str


@router.get("/health", response_model=HealthResponse, summary="Liveness check")
async def health() -> HealthResponse:
    """No DB call — always responds instantly even during cold start."""
    from app.core.config import get_settings
    settings = get_settings()
    return HealthResponse(status="ok", env=settings.app_env)


@router.get("/ping", summary="Minimal keepalive — no DB, no auth")
async def ping() -> dict:
    """UptimeRobot / keepalive target. Returns instantly."""
    return {"ping": "pong"}
