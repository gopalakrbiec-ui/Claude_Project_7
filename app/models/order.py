from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class OrderStatus(str, enum.Enum):
    created = "created"
    queued = "queued"
    moderating = "moderating"
    generating = "generating"
    done = "done"
    rejected = "rejected"   # blocked by safety gate
    refunded = "refunded"
    failed = "failed"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.user_id", ondelete="SET NULL"), index=True
    )
    template_id: Mapped[int] = mapped_column(
        ForeignKey("templates.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Customer-supplied generation parameters
    input_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status"),
        nullable=False,
        default=OrderStatus.created,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    user: Mapped[User] = relationship("User", back_populates="orders")
    agent: Mapped[AgentProfile | None] = relationship("AgentProfile", back_populates="orders")
    template: Mapped[Template] = relationship("Template", back_populates="orders")
    generation_jobs: Mapped[list[GenerationJob]] = relationship(
        "GenerationJob", back_populates="order"
    )


from app.models.agent import AgentProfile  # noqa: E402
from app.models.generation_job import GenerationJob  # noqa: E402
from app.models.template import Template  # noqa: E402
from app.models.user import User  # noqa: E402
