from __future__ import annotations

from pydantic import BaseModel


class TemplateOut(BaseModel):
    id: int
    name: str
    language: str
    theme: str
    base_price_paise: int
    asset_keys: dict
    preview_url: str | None = None  # convenience field extracted from asset_keys

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_with_preview(cls, template: object) -> "TemplateOut":
        obj = cls.model_validate(template)
        obj.preview_url = obj.asset_keys.get("preview_url")
        return obj
