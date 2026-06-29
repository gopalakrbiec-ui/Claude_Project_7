from __future__ import annotations

from pydantic import BaseModel


class TemplateOut(BaseModel):
    id: int
    name: str
    category: str
    theme: str
    image_url: str | None = None
    scene_description: str | None = None
    base_price_paise: int

    model_config = {"from_attributes": True}
