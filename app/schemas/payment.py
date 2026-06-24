from __future__ import annotations

from pydantic import BaseModel, Field


class CreateOrderIn(BaseModel):
    amount_paise: int = Field(..., gt=0, description="Amount in paise (integer, never float)")


class CreateOrderOut(BaseModel):
    payment_id: int          # our DB row id
    gateway_order_id: str    # passed to Razorpay SDK
    amount_paise: int
    currency: str
    key_id: str              # Razorpay publishable key — safe for client


class WebhookAck(BaseModel):
    status: str = "ok"
