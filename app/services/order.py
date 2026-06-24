from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.queue import JobQueue
from app.models.ledger import LedgerReason
from app.models.order import Order
from app.repositories.order import OrderRepository
from app.repositories.template import TemplateRepository
from app.services.credits import CreditsService, InsufficientBalanceError, LedgerRef

logger = logging.getLogger(__name__)


class TemplateNotFoundError(Exception):
    pass


class OrderService:
    """
    Orchestrates order creation: template validation → credit debit → job enqueue.

    Atomicity guarantee
    -------------------
    The credit debit and the order row are written in a **single DB transaction**.
    The Arq enqueue happens after commit (best-effort). If Redis is unavailable
    or the enqueue call throws, the order is already committed with
    status=queued — a reconciliation cron re-enqueues any orders that have
    been stuck in that state for more than ~30 s.

    This means there is a small window where the user's credits are reserved
    but no Arq job exists yet. In that window:
      - The order is visible via GET /orders/{id} with status=queued
      - The worker cron picks it up within 30 s of the Redis failure resolving
      - The user is never double-charged: the debit idempotency key is
        "debit:order:{order.id}" — even if the service method is retried
        end-to-end, the second debit call is a no-op

    What we guarantee absolutely:
      ✅  No debit without an order row  (same transaction, commits together)
      ✅  No order row without a debit   (same transaction, rolls back together)
      ✅  No double-charge on retry      (debit idempotency key)
      ✅  No double-order on retry       (order idempotency key, unique constraint)
    """

    def __init__(
        self,
        session: AsyncSession,
        queue: JobQueue,
    ) -> None:
        self._session = session
        self._queue = queue
        self._order_repo = OrderRepository(session)
        self._template_repo = TemplateRepository(session)
        self._credits = CreditsService(session)

    async def create_order(
        self,
        *,
        user_id: int,
        template_id: int,
        input_payload: dict,
        idempotency_key: str,
    ) -> Order:
        """
        Create an order, debit credits, and enqueue a generation job.

        Raises:
            TemplateNotFoundError    — template_id doesn't exist or is inactive
            InsufficientBalanceError — user can't afford the order
        """
        # ── Idempotency: return existing order on replay ──────────────────────
        existing = await self._order_repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing

        # ── Validate template ─────────────────────────────────────────────────
        template = await self._template_repo.get_active(template_id)
        if template is None:
            raise TemplateNotFoundError(f"Template {template_id} not found or inactive")

        price_paise = template.base_price_paise

        # ── Atomic: debit + order row in one transaction ──────────────────────
        #
        # Step order matters:
        #   1. advisory lock (inside CreditsService) prevents concurrent debits
        #      on the same user from racing
        #   2. balance check — raise before touching the order table
        #   3. order row INSERT — gives us order.id for the debit ref
        #   4. debit ledger entry — references the order we just created
        #   5. commit — both writes land together or neither does
        #
        # We call CreditsService.debit() AFTER inserting the order so the
        # ledger entry's ref_id points at a real order.id. CreditsService
        # acquires the advisory lock on entry, so the balance cannot change
        # between the check and the debit.

        try:
            # Step 1 + 2: acquire lock and check balance before any writes
            # We call _credits._repo.acquire_user_lock() explicitly so the lock
            # is held for the entire create_order transaction, preventing a
            # parallel request from passing the balance check between our check
            # and our order INSERT.
            await self._credits._repo.acquire_user_lock(user_id)
            balance = await self._credits.get_balance(user_id)
            if balance < price_paise:
                raise InsufficientBalanceError(user_id, balance, price_paise)

            # Step 3: insert order row (status=queued)
            order = await self._order_repo.create(
                user_id=user_id,
                template_id=template_id,
                input_payload=input_payload,
                price_paise=price_paise,
                idempotency_key=idempotency_key,
            )

            # Step 4: debit — advisory lock is already held; idempotency_key is
            # stable across retries because it encodes the order's primary key.
            await self._credits.debit(
                user_id=user_id,
                delta_paise=price_paise,
                reason=LedgerReason.spend,
                ref=LedgerRef(ref_type="order", ref_id=order.id),
                idempotency_key=f"debit:order:{order.id}",
            )

            # Step 5: commit — order row and ledger entry land atomically.
            await self._session.commit()

        except IntegrityError:
            # Unique constraint on idempotency_key fired concurrently — safe to
            # return the winner's row.
            await self._session.rollback()
            existing = await self._order_repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing
            raise  # unexpected constraint violation, re-raise

        # ── Enqueue job (best-effort, outside transaction) ────────────────────
        #
        # If this raises, the order is already committed at status=queued.
        # The reconciliation cron will re-enqueue it.
        try:
            await self._queue.enqueue("generate_content", order_id=order.id)
        except Exception:
            logger.exception(
                "Failed to enqueue generation job for order_id=%s — "
                "order is queued in DB and will be picked up by reconciliation cron",
                order.id,
            )

        return order

    async def get_order(self, order_id: int, user_id: int) -> Order | None:
        """
        Return the order if it belongs to the requesting user, else None.
        """
        order = await self._order_repo.get_by_id(order_id)
        if order is None or order.user_id != user_id:
            return None
        return order
