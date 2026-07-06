from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class CreateOrderIn(BaseModel):
    template_id: int
    # User's uploaded photo key (from POST /uploads/photo) — single-photo orders
    user_photo_key: str | None = None
    # Multi-photo orders (2-4 photos) — merged into one composited scene.
    # Takes priority over user_photo_key when both are present.
    user_photo_keys: list[str] | None = Field(default=None, max_length=4)
    # Optional free-text from user (names, event name, date, etc.)
    user_prompt: str | None = Field(default=None, max_length=500)
    # Desired output aspect ratio
    aspect_ratio: Literal["1:1", "9:16", "16:9", "4:3", "3:4"] = "9:16"
    # Client generates this UUID to make double-taps idempotent.
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    # Legacy free-form payload — kept for backward compat
    input_payload: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class OrderOut(BaseModel):
    id: int
    user_id: int
    template_id: int
    template_name: str | None = None  # populated by list endpoint
    price_paise: int
    status: str
    input_payload: dict
    created_at: datetime
    result_url: str | None = None  # presigned URL populated by GET /orders/{id}
    rejection_reason: str | None = None  # populated when status == "rejected"

    model_config = {"from_attributes": True}


class OrderListOut(BaseModel):
    orders: list[OrderOut]
    total: int
    page: int
    limit: int
