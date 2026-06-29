from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.template import Template


class TemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(self, template_id: int) -> Template | None:
        result = await self._session.execute(
            select(Template).where(Template.id == template_id, Template.active.is_(True))
        )
        return result.scalar_one_or_none()

    async def list_active(
        self,
        *,
        language: str | None = None,
        theme: str | None = None,
        category: str | None = None,
        featured: bool | None = None,
        limit: int = 50,
    ) -> list[Template]:
        q = select(Template).where(Template.active.is_(True))
        if language:
            q = q.where(Template.language == language)
        if theme:
            q = q.where(Template.theme == theme)
        if category:
            q = q.where(Template.category == category)
        if featured is not None:
            q = q.where(Template.is_featured.is_(featured))
        q = q.order_by(Template.is_featured.desc(), Template.id).limit(limit)
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def list_grouped(self, *, limit_per_category: int = 20) -> dict[str, list[Template]]:
        """Return active templates grouped by category, featured first within each group."""
        q = (
            select(Template)
            .where(Template.active.is_(True))
            .order_by(Template.category, Template.is_featured.desc(), Template.id)
        )
        result = await self._session.execute(q)
        groups: dict[str, list[Template]] = defaultdict(list)
        for tmpl in result.scalars().all():
            if len(groups[tmpl.category]) < limit_per_category:
                groups[tmpl.category].append(tmpl)
        return dict(groups)
