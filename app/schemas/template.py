from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

_CATEGORY_LABELS: dict[str, str] = {
    "wedding": "Wedding",
    "bollywood": "Bollywood",
    "cricket": "Cricket Glory",
    "royal": "Royal India",
    "birthday": "Birthday",
    "professional": "Professional",
    "festival": "Festival",
    "fashion": "Fashion",
    "family": "Family",
    "childhood": "Childhood",
    "school": "School",
    "college": "College",
    "love": "Love",
    "engagement": "Engagement",
    "pregnancy": "Pregnancy",
    "baby": "Baby",
    "travel": "Travel",
    "career": "Career",
    "traditional": "Traditional",
    "spiritual": "Spiritual",
    "tribute": "Tribute / Memory Restore",
    "fantasy": "Fantasy",
}


class TemplateOut(BaseModel):
    id: int
    name: str
    category: str
    theme: str
    image_url: str | None = None
    scene_description: str | None = None
    negative_prompt: str | None = None
    tags: list[str] = Field(default_factory=list)
    aspect_ratio: str = "9:16"
    base_price_paise: int
    is_featured: bool = False
    # DB stores asset_keys as JSONB dict; normalised to list[str] by _normalise()
    asset_keys: Any = Field(default_factory=list)
    preview_url: str | None = None

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def _normalise(self) -> "TemplateOut":
        """
        Flatten asset_keys to a list of URLs for Flutter.
        DB stores asset_keys as a dict or list; Flutter expects list[str].
        """
        raw = self.__dict__.get("asset_keys") or {}
        if isinstance(raw, dict):
            urls = [v for v in raw.values() if isinstance(v, str) and v.startswith("http")]
        elif isinstance(raw, list):
            urls = [v for v in raw if isinstance(v, str)]
        else:
            urls = []

        # Prepend image_url if not already in list
        if self.image_url and self.image_url not in urls:
            urls = [self.image_url] + urls

        self.asset_keys = urls
        self.preview_url = urls[0] if urls else None
        return self


class TemplateCategoryGroup(BaseModel):
    category: str
    label: str
    templates: list[TemplateOut]
