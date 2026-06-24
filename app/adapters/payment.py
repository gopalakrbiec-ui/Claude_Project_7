from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

from app.core.config import get_settings


@dataclass(frozen=True)
class GatewayOrder:
    """Everything the mobile client needs to open the payment sheet."""
    gateway_order_id: str   # Razorpay order_id — passed to SDK
    amount_paise: int
    currency: str
    key_id: str              # Razorpay publishable key — safe to send to client
    receipt: str


@runtime_checkable
class PaymentGateway(Protocol):
    """
    Adapter interface for payment gateways.

    All money amounts are in integer paise.  Implementations must be
    stateless and safe to call concurrently.
    """

    async def create_order(self, *, amount_paise: int, receipt: str) -> GatewayOrder:
        """Create a payment order on the gateway and return client-facing data."""
        ...

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        """
        Return True iff the webhook signature is authentic.

        Must use a constant-time comparison to prevent timing attacks.
        """
        ...


class RazorpayAdapter:
    """
    Production Razorpay implementation.

    Uses httpx for async HTTP.  Credentials come from pydantic-settings —
    never hardcoded here.
    """

    _BASE = "https://api.razorpay.com/v1"

    def __init__(self) -> None:
        s = get_settings()
        self._key_id = s.razorpay_key_id
        self._key_secret = s.razorpay_key_secret
        self._webhook_secret = s.razorpay_webhook_secret

    async def create_order(self, *, amount_paise: int, receipt: str) -> GatewayOrder:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._BASE}/orders",
                auth=(self._key_id, self._key_secret),
                json={
                    "amount": amount_paise,
                    "currency": "INR",
                    "receipt": receipt,
                    "payment_capture": 1,  # auto-capture on payment
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()

        return GatewayOrder(
            gateway_order_id=data["id"],
            amount_paise=data["amount"],
            currency=data["currency"],
            key_id=self._key_id,
            receipt=data["receipt"],
        )

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        expected = hmac.new(
            self._webhook_secret.encode(),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        # constant-time compare prevents timing oracle attacks
        return hmac.compare_digest(expected, signature)
