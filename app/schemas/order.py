from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CreateOrderIn(BaseModel):
    template_id: int
    # input_payload is free-form metadata: names, dates, language, theme, media_type.
    # Images must be uploaded separately and referenced by URL or storage key — never
    # embed raw bytes here.
    input_payload: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
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
    result_url: str | None = None  # presigned URL populated by GET /orders/{id}

    model_config = {"from_attributes": True}
