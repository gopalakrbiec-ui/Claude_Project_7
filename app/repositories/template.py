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
