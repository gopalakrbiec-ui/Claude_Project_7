from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ledger import CreditLedger, LedgerReason


class LedgerRepository:
    """
    All DB operations for the credit ledger.  No business logic here —
    the service layer enforces invariants (balance >= 0, idempotency semantics).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_balance(self, user_id: int) -> int:
        """Return the current spendable balance in paise.  Always >= 0 by invariant."""
        result = await self._session.execute(
            select(func.coalesce(func.sum(CreditLedger.delta_paise), 0)).where(
                CreditLedger.user_id == user_id
            )
        )
        return int(result.scalar_one())

    async def get_by_idempotency_key(self, key: str) -> CreditLedger | None:
        """Look up an existing ledger entry by its idempotency key."""
        result = await self._session.execute(
            select(CreditLedger).where(CreditLedger.idempotency_key == key)
        )
        return result.scalar_one_or_none()

    async def insert_entry(
        self,
        *,
        user_id: int,
        delta_paise: int,
        reason: LedgerReason,
        ref_type: str | None,
        ref_id: int | None,
        idempotency_key: str,
    ) -> CreditLedger:
        """
        Append a new ledger row.  The caller is responsible for holding the
        per-user advisory lock before calling this so balance can be checked
        safely.  The unique constraint on idempotency_key is the last line of
        defence against double-writes.
        """
        entry = CreditLedger(
            user_id=user_id,
            delta_paise=delta_paise,
            reason=reason,
            ref_type=ref_type,
            ref_id=ref_id,
            idempotency_key=idempotency_key,
        )
        self._session.add(entry)
        await self._session.flush()  # populate entry.id without committing
        return entry

    async def acquire_user_lock(self, user_id: int) -> None:
        """
        Take a PostgreSQL advisory transaction lock scoped to this user_id.

        pg_advisory_xact_lock() blocks until no other transaction holds the
        lock for the same key, then holds it until the surrounding transaction
        commits or rolls back.  This serialises all balance mutations for a
        given user without a dedicated lock-row table or optimistic-retry loop.

        The lock key space is the full int64 range.  We hash user_id into it to
        avoid clashing with advisory locks used elsewhere in the application.
        Prefix 0x_CRED (arbitrary namespace constant) keeps it isolated.
        """
        NAMESPACE = 0x43524544  # ASCII "CRED"
        lock_key = (NAMESPACE << 32) | (user_id & 0xFFFF_FFFF)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key}
        )
