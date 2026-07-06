from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    theme: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # Public URL of the reference/style image shown in the template grid
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Scene description used to craft the generation prompt
    scene_description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Things the generation prompt should actively avoid (deformities, artifacts, etc.)
    # Not currently sent to gpt-image-2 (no negative-prompt param) — captured for
    # future SD-style providers and for prompt-authoring consistency.
    negative_prompt: Mapped[str | None] = mapped_column(String, nullable=True)
    # Free-form search/filter tags, e.g. ["wedding", "outdoor", "sunset"]
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Preferred output aspect ratio for this template
    aspect_ratio: Mapped[str] = mapped_column(String(10), nullable=False, default="9:16")
    # R2 object keys for source assets (background images, overlays, fonts, etc.)
    asset_keys: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    base_price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    is_featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    orders: Mapped[list[Order]] = relationship("Order", back_populates="template")


from app.models.order import Order  # noqa: E402
