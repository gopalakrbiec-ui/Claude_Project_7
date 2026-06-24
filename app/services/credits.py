from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ledger import CreditLedger, LedgerReason
from app.repositories.ledger import LedgerRepository


class InsufficientBalanceError(Exception):
    """Raised when a debit would take balance below zero."""

    def __init__(self, user_id: int, available: int, requested: int) -> None:
        self.user_id = user_id
        self.available = available
        self.requested = requested
        super().__init__(
            f"User {user_id} has {available} paise but debit requested {requested} paise"
        )


@dataclass(frozen=True)
class LedgerRef:
    ref_type: str
    ref_id: int


class CreditsService:
    """
    All credit / debit operations go through this service.

    Concurrency contract
    --------------------
    Every mutation (credit or debit) runs inside a single DB transaction and
    begins by acquiring a per-user PostgreSQL advisory lock via
    LedgerRepository.acquire_user_lock().  This lock serialises all concurrent
    mutations for the same user — only one transaction can hold it at a time.
    Callers do not need retry logic; they simply block until the lock is free.

    Idempotency contract
    --------------------
    If a row with the given idempotency_key already exists in the ledger, the
    mutation is a no-op: the existing entry is returned unchanged.  This makes
    every write safe to retry after a network error or worker crash.  The DB
    unique constraint is a hard backstop even if the service-layer check is
    somehow bypassed (e.g. two workers racing before either reads the existing
    row); in that case IntegrityError is caught and the existing row is
    returned.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = LedgerRepository(session)

    async def get_balance(self, user_id: int) -> int:
        """Return spendable balance in paise.  No lock needed — read-only."""
        return await self._repo.get_balance(user_id)

    async def credit(
        self,
        *,
        user_id: int,
        delta_paise: int,
        reason: LedgerReason,
        ref: LedgerRef | None = None,
        idempotency_key: str,
    ) -> CreditLedger:
        """
        Add credits to a user's balance.

        delta_paise must be positive.  Returns the (possibly pre-existing)
        ledger entry — idempotent on repeated calls with the same key.
        """
        if delta_paise <= 0:
            raise ValueError(f"credit delta_paise must be positive, got {delta_paise}")

        return await self._mutate(
            user_id=user_id,
            delta_paise=delta_paise,
            reason=reason,
            ref=ref,
            idempotency_key=idempotency_key,
        )

    async def debit(
        self,
        *,
        user_id: int,
        delta_paise: int,
        reason: LedgerReason,
        ref: LedgerRef | None = None,
        idempotency_key: str,
    ) -> CreditLedger:
        """
        Remove credits from a user's balance.

        delta_paise must be positive (the sign is applied internally).
        Raises InsufficientBalanceError if balance would go below zero.
        Returns the ledger entry — idempotent on repeated calls with same key.
        """
        if delta_paise <= 0:
            raise ValueError(f"debit delta_paise must be positive, got {delta_paise}")

        return await self._mutate(
            user_id=user_id,
            delta_paise=-delta_paise,  # stored as negative in ledger
            reason=reason,
            ref=ref,
            idempotency_key=idempotency_key,
            check_balance=True,
            requested_paise=delta_paise,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _mutate(
        self,
        *,
        user_id: int,
        delta_paise: int,
        reason: LedgerReason,
        ref: LedgerRef | None,
        idempotency_key: str,
        check_balance: bool = False,
        requested_paise: int = 0,
    ) -> CreditLedger:
        """
        Core write path shared by credit() and debit().

        Execution order inside a single transaction:
          1. Acquire per-user advisory lock  →  serialises concurrent mutations
          2. Check idempotency table         →  early-exit if already done
          3. (debit only) check balance      →  reject if insufficient
          4. Insert ledger row               →  append-only write
          5. Commit (caller's responsibility via the session context)

        The session must be managed by the caller (e.g. via get_db() dependency
        injection).  We do NOT commit here; that lets callers compose this with
        other writes in one atomic unit (e.g. order status + debit together).
        """
        # Step 1 — advisory lock: no other transaction for this user can
        # proceed past this point until we commit or roll back.
        await self._repo.acquire_user_lock(user_id)

        # Step 2 — idempotency check inside the lock.
        existing = await self._repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing

        # Step 3 — balance guard for debits.
        if check_balance:
            balance = await self._repo.get_balance(user_id)
            if balance < requested_paise:
                raise InsufficientBalanceError(user_id, balance, requested_paise)

        # Step 4 — insert (unique constraint is last-resort protection).
        try:
            return await self._repo.insert_entry(
                user_id=user_id,
                delta_paise=delta_paise,
                reason=reason,
                ref_type=ref.ref_type if ref else None,
                ref_id=ref.ref_id if ref else None,
                idempotency_key=idempotency_key,
            )
        except IntegrityError:
            # Another concurrent writer committed the same idempotency_key
            # between our check and our insert (possible under high concurrency
            # even with the advisory lock if the lock namespace collides —
            # extremely unlikely but handled defensively).
            await self._session.rollback()
            existing = await self._repo.get_by_idempotency_key(idempotency_key)
            assert existing is not None  # must exist if IntegrityError was on this key
            return existing
