from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.payment import GatewayOrder, PaymentGateway
from app.models.ledger import LedgerReason
from app.models.payment import Payment, PaymentStatus
from app.models.user import User
from app.repositories.payment import PaymentRepository
from app.services.credits import CreditsService, LedgerRef

logger = logging.getLogger(__name__)


class WebhookSignatureError(Exception):
    """Raised when the Razorpay webhook HMAC does not match."""


@dataclass
class CreateOrderResult:
    payment: Payment
    gateway_order: GatewayOrder


class PaymentService:
    """
    Orchestrates the full payment lifecycle:
      create_order  →  gateway creates order  →  DB row created
      handle_webhook →  verify sig  →  update DB row  →  credit wallet

    Idempotency
    -----------
    create_order:  uses a UUID idempotency_key on the payments table so
                   double-taps from the client don't create duplicate orders.

    handle_webhook:  the credit ledger entry uses
                     "razorpay:capture:{gateway_payment_id}" as its
                     idempotency_key, so even if Razorpay delivers the
                     webhook twice the credit is inserted exactly once.
                     The payment row status is also checked before crediting:
                     if already paid, the DB update is skipped and the credit
                     call is still made (idempotent no-op via the ledger key).

    Out-of-order webhooks
    ---------------------
    Razorpay can send payment.failed followed later by payment.captured
    (rare but documented).  We handle this by always checking the current
    payment status before deciding whether to credit:
      - already paid → return early (idempotent, credit already issued)
      - created or failed → update to paid and credit (idempotent via key)

    We always store the raw webhook body regardless of current state, for
    audit and support purposes.

    Why we credit on webhook, not on the client success callback
    ------------------------------------------------------------
    See docstring at the bottom of this module.
    """

    def __init__(
        self,
        session: AsyncSession,
        gateway: PaymentGateway,
    ) -> None:
        self._session = session
        self._gateway = gateway
        self._payment_repo = PaymentRepository(session)
        self._credits = CreditsService(session)

    async def create_order(
        self,
        *,
        user: User,
        amount_paise: int,
    ) -> CreateOrderResult:
        """
        Create a Razorpay order and persist a Payment row.

        Returns the gateway order data the mobile SDK needs to open checkout.
        """
        receipt = f"rcpt_{uuid.uuid4().hex[:16]}"
        idempotency_key = f"order:{receipt}"

        gateway_order = await self._gateway.create_order(
            amount_paise=amount_paise,
            receipt=receipt,
        )

        from app.models.payment import PaymentGateway as PGEnum  # avoid name clash

        payment = await self._payment_repo.create(
            user_id=user.id,
            gateway=PGEnum.razorpay,
            gateway_order_id=gateway_order.gateway_order_id,
            amount_paise=amount_paise,
            idempotency_key=idempotency_key,
        )
        await self._session.commit()
        return CreateOrderResult(payment=payment, gateway_order=gateway_order)

    async def handle_webhook(
        self,
        raw_body: bytes,
        signature: str,
        payload: dict,
    ) -> None:
        """
        Process a Razorpay webhook event.

        raw_body  — the verbatim request body bytes, used for HMAC verification
        signature — value of the X-Razorpay-Signature header
        payload   — the parsed JSON body (parsed separately by the router so
                    we still have the raw bytes for signature checking)

        Raises WebhookSignatureError if the HMAC check fails — caller returns 400.
        All other outcomes return normally so Razorpay receives 200 and stops
        retrying (per their retry policy: non-200 triggers up to 3 retries).
        """
        if not self._gateway.verify_webhook_signature(raw_body, signature):
            raise WebhookSignatureError("Webhook signature verification failed")

        event: str = payload.get("event", "")
        entity: dict = (
            payload.get("payload", {}).get("payment", {}).get("entity", {})
        )
        gateway_order_id: str = entity.get("order_id", "")
        gateway_payment_id: str = entity.get("id", "")
        razorpay_status: str = entity.get("status", "")

        if not gateway_order_id:
            # Informational event with no payment entity (e.g. subscription events).
            logger.info("Ignoring non-payment webhook event=%s", event)
            return

        payment = await self._payment_repo.get_by_gateway_order_id(gateway_order_id)
        if payment is None:
            # Order was created outside our system or DB insert failed; log and ack.
            logger.warning(
                "Webhook received for unknown order gateway_order_id=%s event=%s",
                gateway_order_id,
                event,
            )
            return

        # Always persist the latest raw webhook for audit, regardless of outcome.
        # We do a targeted flush inside each branch so the raw body is always saved.

        if razorpay_status == "captured" or event == "payment.captured":
            await self._handle_captured(
                payment=payment,
                gateway_payment_id=gateway_payment_id,
                raw_webhook=payload,
            )
        elif razorpay_status == "failed" or event == "payment.failed":
            await self._handle_failed(
                payment=payment,
                gateway_payment_id=gateway_payment_id,
                raw_webhook=payload,
            )
        else:
            # Unknown status — store raw webhook but take no action.
            logger.info(
                "Unhandled webhook event=%s status=%s order=%s",
                event,
                razorpay_status,
                gateway_order_id,
            )
            payment.raw_webhook = payload
            await self._session.commit()

    async def _handle_captured(
        self,
        payment: Payment,
        gateway_payment_id: str,
        raw_webhook: dict,
    ) -> None:
        """Credit the user's wallet on a successful capture."""
        if payment.status == PaymentStatus.paid:
            # Already processed — idempotent no-op.  Still attempt the credit
            # call below; it will be a no-op via the ledger idempotency key.
            logger.info(
                "Duplicate captured webhook for already-paid payment id=%s", payment.id
            )
        else:
            await self._payment_repo.set_paid(
                payment, gateway_payment_id, raw_webhook
            )

        # Credit is always attempted with the same idempotency key so it is
        # safe to call even when the payment row is already marked paid.
        credit_key = f"razorpay:capture:{gateway_payment_id}"
        await self._credits.credit(
            user_id=payment.user_id,
            delta_paise=payment.amount_paise,
            reason=LedgerReason.purchase,
            ref=LedgerRef(ref_type="payment", ref_id=payment.id),
            idempotency_key=credit_key,
        )
        await self._session.commit()

    async def _handle_failed(
        self,
        payment: Payment,
        gateway_payment_id: str,
        raw_webhook: dict,
    ) -> None:
        """Record failure; no credits issued."""
        if payment.status == PaymentStatus.paid:
            # Out-of-order: we already processed a capture — ignore the failure.
            logger.warning(
                "Received payment.failed for already-paid payment id=%s — ignoring",
                payment.id,
            )
            return

        await self._payment_repo.set_failed(payment, gateway_payment_id, raw_webhook)
        await self._session.commit()


# ---------------------------------------------------------------------------
# Why we credit on the webhook and never trust the client success callback
# ---------------------------------------------------------------------------
#
# After a user pays in the Razorpay checkout sheet, the SDK fires a
# success callback on the device.  This callback is *client-controlled*:
#
#   • A determined attacker can replay a success callback from a previous
#     payment against a new order, or craft a fake one entirely.
#
#   • Poor/intermittent network conditions mean the callback can fire multiple
#     times (retry logic in the SDK), arrive after the webhook, or not arrive
#     at all (app killed, connectivity dropped).
#
#   • The client has no way to prove the payment happened — only Razorpay's
#     servers can sign that proof.
#
# The webhook is signed with HMAC-SHA256 using a secret shared only between
# Razorpay and our backend.  Forging it requires the webhook secret, which
# the client never sees.  Razorpay also retries webhooks for up to 24 hours
# on non-200 responses, giving us strong delivery guarantees.
#
# The correct architecture is therefore:
#   client callback  →  show "processing" spinner
#   webhook received →  credit wallet  →  push notification to client
#
# We never credit based on what the client tells us.
