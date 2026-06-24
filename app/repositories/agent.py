from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentProfile
from app.models.ledger import CreditLedger, LedgerReason


class AgentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_user_id(self, user_id: int) -> AgentProfile | None:
        result = await self._session.execute(
            select(AgentProfile).where(AgentProfile.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_commission_entries(
        self, agent_user_id: int, *, limit: int = 20
    ) -> list[CreditLedger]:
        """Return the most recent commission ledger entries for this agent."""
        result = await self._session.execute(
            select(CreditLedger)
            .where(
                CreditLedger.user_id == agent_user_id,
                CreditLedger.reason == LedgerReason.commission,
            )
            .order_by(CreditLedger.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_commission_total(self, agent_user_id: int) -> int:
        """Return the lifetime commission earned in paise (always >= 0)."""
        from sqlalchemy import func

        result = await self._session.execute(
            select(func.coalesce(func.sum(CreditLedger.delta_paise), 0)).where(
                CreditLedger.user_id == agent_user_id,
                CreditLedger.reason == LedgerReason.commission,
            )
        )
        return int(result.scalar_one())
