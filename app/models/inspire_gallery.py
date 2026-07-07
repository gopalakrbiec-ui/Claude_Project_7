from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InspireGalleryItem(Base):
    """
    Pre-generated AI styling inspiration images (celebrity/billionaire
    styling, wedding dresses, grooming, etc). Generated once via
    scripts/generate_inspire_gallery.py — never generated live per-request.

    Using AI-generated images here (rather than real celebrity/editorial
    photos) sidesteps publicity-rights and editorial-licensing risk entirely.
    """

    __tablename__ = "inspire_gallery"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    image_url: Mapped[str] = mapped_column(String(500), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
