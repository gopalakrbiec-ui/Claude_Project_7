from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class LedgerReason(str, enum.Enum):
    purchase = "purchase"      # credits bought via payment
    spend = "spend"            # credits consumed by an order
    refund = "refund"          # credits returned after rejection / failure
    commission = "commission"  # commission credited to an agent


class CreditLedger(Base):
    """
    Append-only ledger of credit mutations.

    Balance for a user = SELECT SUM(delta_paise) FROM credit_ledger WHERE user_id = ?

    Rows are NEVER updated or deleted. The idempotency_key unique constraint
    guarantees that retried writes don't double-count. All negative deltas
    (spends) are written inside a DB transaction that first checks
    SUM(delta_paise) >= abs(spend) — enforced in the service layer.
    """

    __tablename__ = "credit_ledger"

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_credit_ledger_idempotency_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Signed integer in paise.  Positive = credit in, negative = credit out.
    delta_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[LedgerReason] = mapped_column(
        Enum(LedgerReason, name="ledger_reason"), nullable=False, index=True
    )
    # Polymorphic reference: (ref_type, ref_id) points at the causative row
    # e.g. ("payment", 42) or ("order", 7).  Not a FK so ref_type is flexible.
    ref_type: Mapped[str | None] = mapped_column(String(50))
    ref_id: Mapped[int | None] = mapped_column(BigInteger)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    user: Mapped[User] = relationship("User", back_populates="ledger_entries")


from app.models.user import User  # noqa: E402
