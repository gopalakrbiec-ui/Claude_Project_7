from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    env: str


@router.get("/health", response_model=HealthResponse, summary="Liveness check")
async def health() -> HealthResponse:
    from app.core.config import get_settings

    settings = get_settings()
    return HealthResponse(status="ok", env=settings.app_env)
