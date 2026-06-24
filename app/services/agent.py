from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ledger import CreditLedger, LedgerReason
from app.models.order import Order, OrderStatus
from app.repositories.agent import AgentRepository
from app.services.credits import CreditsService, LedgerRef

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommissionEntry:
    id: int
    delta_paise: int
    ref_type: str | None
    ref_id: int | None
    created_at: datetime


@dataclass(frozen=True)
class EarningsResult:
    total_commission_paise: int
    entries: list[CommissionEntry]


class NotAnAgentError(Exception):
    pass


class AgentService:
    """
    Handles agent commission crediting and earnings queries.

    Commission lives on the same append-only ledger as every other credit
    mutation (purchases, spends, refunds).  There is no separate commissions
    table — the ledger reason column distinguishes entry types, and the
    balance for any user is always SUM(delta_paise) across ALL reasons.
    This means:

      • An agent's *wallet* balance includes commissions they haven't
        withdrawn yet — they can spend those credits just like a consumer.
      • The *earnings view* filters on reason=commission to show only
        commissions, not purchases the agent made for themselves.
      • Idempotency is enforced by the same unique constraint on
        idempotency_key that protects every other ledger write.

    Why this is correct
    -------------------
    When an order completes, the pipeline commits order.status=done and the
    commission ledger row in one transaction (see workers/jobs.py).  If the
    worker crashes before that commit, the order stays at status=generating,
    the terminal-state guard doesn't fire, and a retry re-runs the final
    upload + commission + commit.  If the worker crashes after the commit,
    the idempotency key prevents a double-pay on any subsequent (hypothetical)
    retry.  So commission is paid *exactly once* regardless of crash timing.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._agent_repo = AgentRepository(session)
        self._credits = CreditsService(session)

    async def pay_commission(self, order: Order) -> CreditLedger | None:
        """
        Credit the agent's commission for a completed order.

        Returns the ledger entry (new or pre-existing on replay).
        Returns None if:
          - The order has no agent_id (consumer order placed directly)
          - The agent's commission_rate_bps is 0
          - The computed commission rounds to 0 paise
          - The order is not in status=done (guards against misuse)

        MUST be called inside an open transaction; the caller commits.
        The idempotency key "commission:{order_id}" makes this safe to call
        multiple times with the same order.
        """
        if order.agent_id is None:
            return None

        if order.status != OrderStatus.done:
            # Only pay commission on successfully completed orders.
            # Rejected / refunded / failed orders earn nothing.
            logger.debug(
                "pay_commission: order %s status=%s — skipping", order.id, order.status
            )
            return None

        agent = await self._agent_repo.get_by_user_id(order.agent_id)
        if agent is None:
            logger.warning(
                "pay_commission: agent_id=%s not found for order %s — skipping",
                order.agent_id,
                order.id,
            )
            return None

        # Integer arithmetic only — never float for money
        commission_paise = order.price_paise * agent.commission_rate_bps // 10_000

        if commission_paise <= 0:
            logger.debug(
                "pay_commission: order %s computed commission=%d paise — skipping",
                order.id,
                commission_paise,
            )
            return None

        entry = await self._credits.credit(
            user_id=order.agent_id,
            delta_paise=commission_paise,
            reason=LedgerReason.commission,
            ref=LedgerRef(ref_type="order", ref_id=order.id),
            idempotency_key=f"commission:{order.id}",
        )

        logger.info(
            "Commission credited: agent_id=%s order_id=%s paise=%d (rate=%d bps)",
            order.agent_id,
            order.id,
            commission_paise,
            agent.commission_rate_bps,
        )
        return entry

    async def get_earnings(self, agent_user_id: int, *, entries_limit: int = 20) -> EarningsResult:
        """
        Return total lifetime commission earned and recent ledger entries
        for the given agent user.

        Raises NotAnAgentError if the user has no agent profile.
        """
        agent = await self._agent_repo.get_by_user_id(agent_user_id)
        if agent is None:
            raise NotAnAgentError(f"User {agent_user_id} has no agent profile")

        total = await self._agent_repo.get_commission_total(agent_user_id)
        raw_entries = await self._agent_repo.get_commission_entries(
            agent_user_id, limit=entries_limit
        )

        entries = [
            CommissionEntry(
                id=e.id,
                delta_paise=e.delta_paise,
                ref_type=e.ref_type,
                ref_id=e.ref_id,
                created_at=e.created_at,
            )
            for e in raw_entries
        ]

        return EarningsResult(total_commission_paise=total, entries=entries)
