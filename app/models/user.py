from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class UserRole(str, enum.Enum):
    consumer = "consumer"
    agent = "agent"
    admin = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    preferred_language: Mapped[str] = mapped_column(String(10), nullable=False, default="hi")
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), nullable=False, default=UserRole.consumer
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships (back-populated from child tables)
    agent_profile: Mapped[AgentProfile | None] = relationship(
        "AgentProfile", back_populates="user", uselist=False
    )
    ledger_entries: Mapped[list[CreditLedger]] = relationship(
        "CreditLedger", back_populates="user"
    )
    payments: Mapped[list[Payment]] = relationship("Payment", back_populates="user")
    orders: Mapped[list[Order]] = relationship("Order", back_populates="user")


# Avoid circular imports by importing sibling models at module level after Base is ready.
from app.models.agent import AgentProfile  # noqa: E402
from app.models.ledger import CreditLedger  # noqa: E402
from app.models.order import Order  # noqa: E402
from app.models.payment import Payment  # noqa: E402
