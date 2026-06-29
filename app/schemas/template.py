from __future__ import annotations

from pydantic import BaseModel, model_validator


class TemplateOut(BaseModel):
    id: int
    name: str
    category: str
    theme: str
    image_url: str | None = None
    scene_description: str | None = None
    base_price_paise: int
    asset_keys: dict = {}
    preview_url: str | None = None  # convenience alias

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def _backfill_asset_keys(self) -> "TemplateOut":
        """Keep asset_keys.preview_url in sync with image_url for Flutter compat."""
        if self.image_url and not self.asset_keys.get("preview_url"):
            self.asset_keys = {**self.asset_keys, "preview_url": self.image_url}
        self.preview_url = self.asset_keys.get("preview_url") or self.image_url
        return self
