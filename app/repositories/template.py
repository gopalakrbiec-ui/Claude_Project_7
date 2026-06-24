from __future__ import annotations

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
        limit: int = 50,
    ) -> list[Template]:
        q = select(Template).where(Template.active.is_(True))
        if language:
            q = q.where(Template.language == language)
        if theme:
            q = q.where(Template.theme == theme)
        q = q.order_by(Template.id).limit(limit)
        result = await self._session.execute(q)
        return list(result.scalars().all())
