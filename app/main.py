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
    from app.api.auth import router as auth_router
    from app.api.credits import router as credits_router
    from app.api.templates import router as templates_router
    from app.api.payments import router as payments_router
    from app.api.orders import router as orders_router
    from app.api.agents import router as agents_router

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(credits_router)
    app.include_router(templates_router)
    app.include_router(payments_router)
    app.include_router(orders_router)
    app.include_router(agents_router)

    return app


app = create_app()
