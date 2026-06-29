from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.repositories.template import TemplateRepository
from app.schemas.template import TemplateCategoryGroup, TemplateOut, _CATEGORY_LABELS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/templates", tags=["templates"])


@router.get("", response_model=list[TemplateOut])
async def list_templates(
    category: Annotated[str | None, Query(description="e.g. wedding, birthday, fashion, family, festival")] = None,
    theme: Annotated[str | None, Query(description="e.g. floral, royal, bridal")] = None,
    featured: Annotated[bool | None, Query(description="Return only featured templates")] = None,
    db: AsyncSession = Depends(get_db),
) -> list[TemplateOut]:
    """List active templates, optionally filtered by category, theme, or featured flag."""
    try:
        repo = TemplateRepository(db)
        templates = await repo.list_active(category=category, theme=theme, featured=featured)
        return [TemplateOut.model_validate(t) for t in templates]
    except Exception:
        logger.exception("Failed to fetch templates")
        return []


@router.get("/grouped", response_model=list[TemplateCategoryGroup])
async def list_templates_grouped(
    db: AsyncSession = Depends(get_db),
) -> list[TemplateCategoryGroup]:
    """Return active templates grouped by category for horizontal-scroll UI."""
    try:
        repo = TemplateRepository(db)
        groups = await repo.list_grouped()
        result: list[TemplateCategoryGroup] = []
        for category, templates in groups.items():
            result.append(
                TemplateCategoryGroup(
                    category=category,
                    label=_CATEGORY_LABELS.get(category, category.title()),
                    templates=[TemplateOut.model_validate(t) for t in templates],
                )
            )
        # Featured categories first: wedding > birthday > festival > fashion > family > others
        _ORDER = list(_CATEGORY_LABELS.keys())
        result.sort(key=lambda g: _ORDER.index(g.category) if g.category in _ORDER else len(_ORDER))
        return result
    except Exception:
        logger.exception("Failed to fetch grouped templates")
        return []
