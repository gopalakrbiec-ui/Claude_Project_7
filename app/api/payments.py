from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.payment import RazorpayAdapter
from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.payment import CreateOrderIn, CreateOrderOut, VerifyPaymentIn, VerifyPaymentOut, WebhookAck
from app.services.payment import PaymentService, WebhookSignatureError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["payments"])

# Credit top-up packs — amount user pays vs credits they receive (both in paise).
# Bonus credits act as a discount incentive for larger packs.
_CREDIT_PACKS = [
    {"id": "pack_49",  "label": "Starter",  "price_paise": 4900,  "credits_paise": 4900,  "bonus_paise": 0,    "popular": False},
    {"id": "pack_99",  "label": "Standard", "price_paise": 9900,  "credits_paise": 11000, "bonus_paise": 1100, "popular": True},
    {"id": "pack_199", "label": "Pro",       "price_paise": 19900, "credits_paise": 23000, "bonus_paise": 3100, "popular": False},
]


@router.get("/packs", summary="List available credit top-up packs")
async def list_packs() -> dict:
    """Returns the available credit packs for the wallet top-up screen."""
    return {"packs": _CREDIT_PACKS}


def _get_payment_service(db: Annotated[AsyncSession, Depends(get_db)]) -> PaymentService:
    return PaymentService(session=db, gateway=RazorpayAdapter())


@router.post(
    "/create-order",
    response_model=CreateOrderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a Razorpay order for the mobile checkout",
)
async def create_order(
    body: CreateOrderIn,
    current_user: Annotated[User, Depends(get_current_user)],
    svc: Annotated[PaymentService, Depends(_get_payment_service)],
) -> CreateOrderOut:
    result = await svc.create_order(user=current_user, amount_paise=body.amount_paise)
    return CreateOrderOut(
        payment_id=result.payment.id,
        gateway_order_id=result.gateway_order.gateway_order_id,
        amount_paise=result.gateway_order.amount_paise,
        currency=result.gateway_order.currency,
        key_id=result.gateway_order.key_id,
    )


@router.post(
    "/webhook",
    response_model=WebhookAck,
    status_code=status.HTTP_200_OK,
    summary="Razorpay webhook receiver",
    # Exclude from OpenAPI auth requirement — Razorpay signs with HMAC, not JWT
    include_in_schema=True,
)
async def payment_webhook(
    request: Request,
    svc: Annotated[PaymentService, Depends(_get_payment_service)],
) -> WebhookAck:
    """
    Receives Razorpay payment events.

    We must read the raw body BEFORE any JSON parsing because the HMAC is
    computed over the exact bytes Razorpay sent.  Even a single whitespace
    difference would invalidate the signature.

    Always returns 200 so Razorpay stops retrying.  Signature failures return
    400 so the gateway knows something is wrong on our end (misconfigured
    secret) rather than silently swallowing potentially real events.
    """
    raw_body: bytes = await request.body()
    signature: str = request.headers.get("X-Razorpay-Signature", "")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")

    try:
        await svc.handle_webhook(raw_body=raw_body, signature=signature, payload=payload)
    except WebhookSignatureError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook signature verification failed",
        )
    except Exception:
        # Unexpected error — log it, still return 200 to prevent Razorpay retry
        # storm while we investigate.  The raw webhook is already stored in the
        # DB (flushed inside handle_webhook before the error) for manual replay.
        logger.exception("Unexpected error processing webhook body=%s", raw_body[:200])

    return WebhookAck()


@router.post(
    "/verify",
    response_model=VerifyPaymentOut,
    status_code=status.HTTP_200_OK,
    summary="Client-side Razorpay payment verification",
)
async def verify_payment(
    body: VerifyPaymentIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VerifyPaymentOut:
    """
    Called by the Flutter app after Razorpay SDK returns a successful payment.
    Verifies the HMAC signature and credits the user's wallet.
    Idempotent — safe to call multiple times with the same payment ID.
    """
    import hashlib
    import hmac as hmac_lib
    from app.core.config import get_settings
    from app.services.credits import CreditsService
    from app.repositories.payment import PaymentRepository
    from app.models.ledger import LedgerReason
    from app.services.credits import LedgerRef

    settings = get_settings()

    # Verify HMAC: HMAC-SHA256(razorpay_order_id + "|" + razorpay_payment_id, key_secret)
    # Uses razorpay_key_secret (API key secret), NOT the webhook secret — they are different.
    if not settings.razorpay_key_secret:
        logger.error("RAZORPAY_KEY_SECRET is not configured — cannot verify payment signature")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Payment verification not configured")
    message = f"{body.razorpay_order_id}|{body.razorpay_payment_id}".encode()
    expected = hmac_lib.new(
        settings.razorpay_key_secret.encode(), message, hashlib.sha256
    ).hexdigest()
    if not hmac_lib.compare_digest(expected, body.razorpay_signature):
        logger.warning("Payment signature mismatch for order=%s payment=%s", body.razorpay_order_id, body.razorpay_payment_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payment signature")

    payment_repo = PaymentRepository(db)
    payment = await payment_repo.get_by_gateway_order_id(body.razorpay_order_id)
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment order not found")
    if payment.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    # Credit wallet — idempotent via ledger key
    credits_svc = CreditsService(db)
    credit_key = f"razorpay:capture:{body.razorpay_payment_id}"
    await credits_svc.credit(
        user_id=current_user.id,
        delta_paise=payment.amount_paise,
        reason=LedgerReason.purchase,
        ref=LedgerRef(ref_type="payment", ref_id=payment.id),
        idempotency_key=credit_key,
    )
    from app.models.payment import PaymentStatus
    if payment.status != PaymentStatus.paid:
        await payment_repo.set_paid(payment, body.razorpay_payment_id, {
            "razorpay_order_id": body.razorpay_order_id,
            "razorpay_payment_id": body.razorpay_payment_id,
        })
    await db.commit()

    balance = await credits_svc.get_balance(current_user.id)
    return VerifyPaymentOut(status="credited", balance_paise=balance)
