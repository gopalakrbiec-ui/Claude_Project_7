from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class CommissionEntryOut(BaseModel):
    id: int
    delta_paise: int
    ref_type: str | None
    ref_id: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class EarningsOut(BaseModel):
    total_commission_paise: int
    entries: list[CommissionEntryOut]
