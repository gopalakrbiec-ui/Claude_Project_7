from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.services.credits import CreditsService

router = APIRouter(prefix="/credits", tags=["credits"])


class BalanceOut(BaseModel):
    balance_paise: int
    balance_rupees: str  # e.g. "49.50" — string avoids float representation errors


@router.get("/balance", response_model=BalanceOut)
async def get_balance(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> BalanceOut:
    """Return the authenticated user's current credit balance."""
    balance = await CreditsService(db).get_balance(current_user.id)
    rupees = balance // 100
    paise_remainder = balance % 100
    return BalanceOut(
        balance_paise=balance,
        balance_rupees=f"{rupees}.{paise_remainder:02d}",
    )
