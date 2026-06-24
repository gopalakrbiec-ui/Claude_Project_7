from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CreateOrderIn(BaseModel):
    template_id: int
    input_payload: dict = Field(default_factory=dict)
    # Client generates this UUID to make double-taps idempotent.
    idempotency_key: str = Field(..., min_length=8, max_length=128)


class OrderOut(BaseModel):
    id: int
    user_id: int
    template_id: int
    price_paise: int
    status: str
    input_payload: dict
    created_at: datetime

    model_config = {"from_attributes": True}
