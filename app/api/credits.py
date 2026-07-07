from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.services.credits import CreditsService

router = APIRouter(prefix="/credits", tags=["credits"])


class BalanceOut(BaseModel):
    balance_paise: int
    balance_rupees: str  # e.g. "49.50" — string avoids float representation errors
    balance_coins: int   # Savi Coins — 1 coin = ₹1 = 100 paise, floor of balance_paise


class CurrencyInfoOut(BaseModel):
    name: str
    symbol: str
    unit_rate_paise: int  # paise per 1 unit of the display currency


@router.get("/currency-info", response_model=CurrencyInfoOut)
async def get_currency_info() -> CurrencyInfoOut:
    """
    Display-currency metadata for the app UI — lets the coin name/rate change
    server-side without an app store release. No auth required.
    """
    return CurrencyInfoOut(name="Savi Coins", symbol="🪙", unit_rate_paise=100)


def _balance_out(balance_paise: int) -> BalanceOut:
    rupees = balance_paise // 100
    paise_remainder = balance_paise % 100
    return BalanceOut(
        balance_paise=balance_paise,
        balance_rupees=f"{rupees}.{paise_remainder:02d}",
        balance_coins=balance_paise // 100,
    )


@router.get("/balance", response_model=BalanceOut)
async def get_balance(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> BalanceOut:
    """Return the authenticated user's current credit balance."""
    balance = await CreditsService(db).get_balance(current_user.id)
    return _balance_out(balance)


class DevAddCreditsIn(BaseModel):
    amount_paise: int = 100000  # default ₹1000 for testing


@router.post("/dev/add", response_model=BalanceOut, summary="Dev-only: seed test credits")
async def dev_add_credits(
    body: DevAddCreditsIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> BalanceOut:
    """
    Seed credits for testing without a real Razorpay payment.
    Only available when bypass_payments=true. Never enabled in production.
    """
    from app.core.config import get_settings
    from app.models.ledger import LedgerReason
    from app.services.credits import LedgerRef
    import uuid

    settings = get_settings()
    if not settings.bypass_payments:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dev credit endpoint only available when BYPASS_PAYMENTS=true",
        )

    svc = CreditsService(db)
    await svc.credit(
        user_id=current_user.id,
        delta_paise=body.amount_paise,
        reason=LedgerReason.purchase,
        ref=LedgerRef(ref_type="dev", ref_id=0),
        idempotency_key=f"dev:{current_user.id}:{uuid.uuid4()}",
    )
    await db.commit()

    balance = await svc.get_balance(current_user.id)
    return _balance_out(balance)
