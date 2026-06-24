from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.queue import JobQueue, LoggingJobQueue
from app.models.user import User
from app.schemas.order import CreateOrderIn, OrderOut
from app.services.credits import InsufficientBalanceError
from app.services.order import OrderService, TemplateNotFoundError

router = APIRouter(prefix="/orders", tags=["orders"])


def _get_queue() -> JobQueue:
    """
    Returns the active job queue.

    In production, replace with ArqJobQueue backed by a real arq Redis pool.
    Wired via FastAPI dependency injection so tests can override it trivially.
    """
    return LoggingJobQueue()


def _get_order_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    queue: Annotated[JobQueue, Depends(_get_queue)],
) -> OrderService:
    return OrderService(session=db, queue=queue)


@router.post(
    "",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a generation order",
)
async def create_order(
    body: CreateOrderIn,
    current_user: Annotated[User, Depends(get_current_user)],
    svc: Annotated[OrderService, Depends(_get_order_service)],
) -> OrderOut:
    try:
        order = await svc.create_order(
            user_id=current_user.id,
            template_id=body.template_id,
            input_payload=body.input_payload,
            idempotency_key=body.idempotency_key,
        )
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InsufficientBalanceError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "insufficient_balance",
                "available_paise": exc.available,
                "required_paise": exc.requested,
            },
        )
    return OrderOut.model_validate(order)


@router.get(
    "/{order_id}",
    response_model=OrderOut,
    summary="Poll order status",
)
async def get_order(
    order_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    svc: Annotated[OrderService, Depends(_get_order_service)],
) -> OrderOut:
    order = await svc.get_order(order_id, current_user.id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return OrderOut.model_validate(order)
