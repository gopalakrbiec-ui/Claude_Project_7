from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    theme: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # R2 object keys for source assets (background images, overlays, fonts, etc.)
    asset_keys: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    base_price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    orders: Mapped[list[Order]] = relationship("Order", back_populates="template")


from app.models.order import Order  # noqa: E402
