from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
        description="AI-generated wedding & life-event content for India.",
        version="0.2.0",
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        lifespan=lifespan,
    )

    # In production, restrict CORS to known origins via ALLOWED_ORIGINS env var.
    # For local dev / Railway preview, wildcard is acceptable.
    allowed_origins = (
        ["*"] if not settings.is_production
        else [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Safely serialize errors — binary fields in the request body can cause
        # UnicodeDecodeError in FastAPI's default handler (e.g. image bytes in JSON).
        def _safe(v: object) -> object:
            if isinstance(v, bytes):
                return f"<binary {len(v)} bytes>"
            if isinstance(v, dict):
                return {k: _safe(val) for k, val in v.items()}
            if isinstance(v, list):
                return [_safe(i) for i in v]
            return v

        errors = [_safe(e) for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    # Routers
    from app.api.health import router as health_router
    from app.api.auth import router as auth_router
    from app.api.credits import router as credits_router
    from app.api.templates import router as templates_router
    from app.api.payments import router as payments_router
    from app.api.orders import router as orders_router
    from app.api.agents import router as agents_router
    from app.api.uploads import router as uploads_router
    from app.api.tools import router as tools_router
    from app.api.inspire import router as inspire_router
    from app.api.prompts import router as prompts_router

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(credits_router)
    app.include_router(templates_router)
    app.include_router(payments_router)
    app.include_router(orders_router)
    app.include_router(agents_router)
    app.include_router(uploads_router)
    app.include_router(tools_router)
    app.include_router(inspire_router)
    app.include_router(prompts_router)

    # Admin dashboard — no-op unless ADMIN_PASSWORD is set in the environment
    from app.admin import setup_admin
    from app.core.database import get_engine
    setup_admin(app, get_engine())

    return app


app = create_app()
