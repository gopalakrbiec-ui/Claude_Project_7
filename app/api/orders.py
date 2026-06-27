from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.queue import ArqJobQueue, JobQueue, LoggingJobQueue
from app.models.user import User
from app.repositories.generation_job import GenerationJobRepository
from app.schemas.order import CreateOrderIn, OrderOut
from app.services.credits import InsufficientBalanceError
from app.services.order import OrderService, TemplateNotFoundError

router = APIRouter(prefix="/orders", tags=["orders"])


async def _get_queue() -> JobQueue:
    from app.core.config import get_settings
    settings = get_settings()
    if settings.app_env in ("production", "staging"):
        import arq
        from arq.connections import RedisSettings
        pool = await arq.create_pool(RedisSettings.from_dsn(str(settings.redis_url)))
        return ArqJobQueue(pool)
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
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrderOut:
    order = await svc.get_order(order_id, current_user.id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    out = OrderOut.model_validate(order)
    if order.status == "done":
        out.result_url = await _presign_result(db, order_id)
    return out


async def _presign_result(db: AsyncSession, order_id: int) -> str | None:
    from app.core.config import get_settings
    from app.adapters.storage import S3StorageAdapter

    settings = get_settings()
    if not settings.s3_endpoint_url or not settings.s3_access_key_id:
        return None

    job_repo = GenerationJobRepository(db)
    job = await job_repo.get_by_order_id(order_id)
    if not job or not job.output_keys:
        return None

    key: str | None = job.output_keys.get("image") or job.output_keys.get("video")
    if not key:
        return None

    storage = S3StorageAdapter(
        endpoint_url=str(settings.s3_endpoint_url),
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
        bucket_name=settings.s3_bucket_name,
    )
    return storage.presign(key, expires_in=3600)
