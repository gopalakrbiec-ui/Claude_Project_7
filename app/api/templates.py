from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.repositories.template import TemplateRepository
from app.schemas.template import TemplateOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/templates", tags=["templates"])


@router.get("", response_model=list[TemplateOut])
async def list_templates(
    language: Annotated[str | None, Query(description="Filter by language code, e.g. hi, en, te")] = None,
    theme: Annotated[str | None, Query(description="Filter by theme, e.g. floral, classic")] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[TemplateOut]:
    """List active templates, optionally filtered by language and/or theme."""
    try:
        repo = TemplateRepository(db)
        templates = await repo.list_active(language=language, theme=theme)
        return [TemplateOut.model_validate(t) for t in templates]
    except Exception:
        logger.exception("Failed to fetch templates")
        return []
