from __future__ import annotations

from pydantic import BaseModel


class TemplateOut(BaseModel):
    id: int
    name: str
    language: str
    theme: str
    base_price_paise: int
    asset_keys: dict

    model_config = {"from_attributes": True}
