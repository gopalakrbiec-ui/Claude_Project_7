from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.user import User, UserRole
from app.schemas.agent import CommissionEntryOut, EarningsOut
from app.services.agent import AgentService, EarningsResult, NotAnAgentError

router = APIRouter(prefix="/agents", tags=["agents"])


def _require_agent(current_user: User) -> User:
    """Shared guard: raises 403 if the caller is not an agent-role user."""
    if current_user.role != UserRole.agent:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint is only available to agent accounts",
        )
    return current_user


def _get_agent_service(db: Annotated[AsyncSession, Depends(get_db)]) -> AgentService:
    return AgentService(session=db)


@router.get(
    "/me/earnings",
    response_model=EarningsOut,
    summary="Agent earnings summary",
    description=(
        "Returns the agent's lifetime total commission earned and their 20 most "
        "recent commission ledger entries.  Only accessible by users with role=agent."
    ),
)
async def get_my_earnings(
    current_user: Annotated[User, Depends(get_current_user)],
    svc: Annotated[AgentService, Depends(_get_agent_service)],
) -> EarningsOut:
    _require_agent(current_user)

    try:
        result: EarningsResult = await svc.get_earnings(current_user.id)
    except NotAnAgentError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent profile not found",
        )

    return EarningsOut(
        total_commission_paise=result.total_commission_paise,
        entries=[
            CommissionEntryOut(
                id=e.id,
                delta_paise=e.delta_paise,
                ref_type=e.ref_type,
                ref_id=e.ref_id,
                created_at=e.created_at,
            )
            for e in result.entries
        ],
    )
