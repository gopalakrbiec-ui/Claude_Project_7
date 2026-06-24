from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class JobStatus(str, enum.Enum):
    pending = "pending"
    moderating = "moderating"
    generating = "generating"
    done = "done"
    rejected = "rejected"
    failed = "failed"


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status"),
        nullable=False,
        default=JobStatus.pending,
        index=True,
    )
    provider: Mapped[str | None] = mapped_column(String(100))
    # Actual cost charged by the provider in paise (recorded after generation).
    cost_paise: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    # R2 object keys for generated outputs (images, video segments, etc.)
    output_keys: Mapped[dict | None] = mapped_column(JSONB)
    # Full moderation API response stored for audit / appeals.
    moderation_result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped[Order] = relationship("Order", back_populates="generation_jobs")


from app.models.order import Order  # noqa: E402
