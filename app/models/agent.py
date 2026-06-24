from __future__ import annotations

import enum

from sqlalchemy import Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class AgentStatus(str, enum.Enum):
    pending = "pending"
    active = "active"
    suspended = "suspended"


class AgentProfile(Base):
    """One-to-one extension of User for agent-role accounts."""

    __tablename__ = "agents"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # commission in basis points: 150 bps = 1.5 %
    commission_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # UPI VPA or bank account details stored as free text; structured later if needed
    payout_upi_vpa: Mapped[str | None] = mapped_column(String(100))
    payout_bank_account: Mapped[str | None] = mapped_column(String(200))
    payout_ifsc: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[AgentStatus] = mapped_column(
        Enum(AgentStatus, name="agent_status"),
        nullable=False,
        default=AgentStatus.pending,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship("User", back_populates="agent_profile")
    orders: Mapped[list[Order]] = relationship("Order", back_populates="agent")


from app.models.order import Order  # noqa: E402
from app.models.user import User  # noqa: E402
