from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.payment import RazorpayAdapter
from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.payment import CreateOrderIn, CreateOrderOut, WebhookAck
from app.services.payment import PaymentService, WebhookSignatureError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["payments"])


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
