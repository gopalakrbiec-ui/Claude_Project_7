from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: nothing to initialise yet — DB engine is lazy, Redis pool is lazy.
    yield
    # Shutdown: close any open connection pools.
    from app.core.database import _engine

    if _engine is not None:
        await _engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="WeddingApp Backend",
        description="AI-generated wedding & life-event content for rural India.",
        version="0.1.0",
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # tighten per environment later
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    from app.api.health import router as health_router

    app.include_router(health_router)

    return app


app = create_app()
