from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Query

from app.api.deps import get_current_user, get_db
from app.core.queue import ArqJobQueue, JobQueue, LoggingJobQueue
from app.models.user import User
from app.repositories.generation_job import GenerationJobRepository
from app.repositories.order import OrderRepository
from app.repositories.template import TemplateRepository
from app.schemas.order import CreateOrderIn, OrderListOut, OrderOut
from app.services.credits import InsufficientBalanceError
from app.services.order import OrderService, TemplateNotFoundError

router = APIRouter(prefix="/orders", tags=["orders"])


async def _get_queue() -> JobQueue:
    from app.core.config import get_settings
    settings = get_settings()
    # Use real Redis queue whenever REDIS_URL is configured — not just in prod/staging.
    # This fixes the "stuck in generating" bug when APP_ENV is not exactly "production".
    if settings.redis_url:
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


@router.get(
    "",
    response_model=OrderListOut,
    summary="List the current user's orders",
)
async def list_orders(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> OrderListOut:
    order_repo = OrderRepository(db)
    template_repo = TemplateRepository(db)
    orders, total = await order_repo.list_for_user(current_user.id, page=page, limit=limit)

    # Batch-fetch template names
    template_ids = {o.template_id for o in orders}
    template_names: dict[int, str] = {}
    for tid in template_ids:
        t = await template_repo.get_active(tid)
        if t:
            template_names[tid] = t.name

    items: list[OrderOut] = []
    for order in orders:
        out = OrderOut.model_validate(order)
        out.template_name = template_names.get(order.template_id)
        if order.status == "done":
            out.result_url = await _presign_result(db, order.id)
        if order.status == "rejected":
            out.rejection_reason = await _fetch_rejection_reason(db, order.id)
        items.append(out)

    return OrderListOut(orders=items, total=total, page=page, limit=limit)


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
        # Merge top-level new fields into input_payload so workers can read them
        payload = dict(body.input_payload)
        if body.user_photo_keys:
            payload["user_photo_keys"] = body.user_photo_keys
        elif body.user_photo_key:
            payload["user_photo_key"] = body.user_photo_key
        if body.user_prompt:
            payload["user_prompt"] = body.user_prompt
        payload["aspect_ratio"] = body.aspect_ratio

        order = await svc.create_order(
            user_id=current_user.id,
            template_id=body.template_id,
            input_payload=payload,
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
    if order.status == "rejected":
        out.rejection_reason = await _fetch_rejection_reason(db, order_id)
    return out


@router.get(
    "/{order_id}/download",
    summary="Get a fresh presigned download URL for a completed order",
)
async def download_order(
    order_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    svc: Annotated[OrderService, Depends(_get_order_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    order = await svc.get_order(order_id, current_user.id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if order.status != "done":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Order not yet complete")
    url = await _presign_result(db, order_id)
    if url is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Output not found")
    return {"result_url": url}


async def _fetch_rejection_reason(db: AsyncSession, order_id: int) -> str | None:
    job_repo = GenerationJobRepository(db)
    job = await job_repo.get_by_order_id(order_id)
    return job.error if job else None


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
