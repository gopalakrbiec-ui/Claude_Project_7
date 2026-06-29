from __future__ import annotations

"""
AI photo-tool endpoints — instant, synchronous transformations.

POST /tools/face-swap   — put the user's face into a template scene
POST /tools/restore     — deblur + enhance an old/low-quality photo
POST /tools/bg-remove   — remove photo background (transparent PNG)
POST /tools/upscale     — 4× Real-ESRGAN upscale

All endpoints:
  • require a logged-in user
  • deduct credits BEFORE calling the provider (same pattern as orders)
  • upload the result to R2 and return a short-lived presigned URL
  • apply the Yadein watermark to the output (except bg-remove)
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.ledger import LedgerReason
from app.models.user import User
from app.services.credits import CreditsService, InsufficientBalanceError, LedgerRef

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tools", tags=["tools"])

# Cost per tool in paise (can later be moved to config / DB)
_COST_FACE_SWAP = 300
_COST_RESTORE = 200
_COST_BG_REMOVE = 150
_COST_UPSCALE = 150


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class FaceSwapIn(BaseModel):
    source_photo_key: str = Field(..., description="R2 key of the user's face photo (from /uploads/photo)")
    target_image_url: str = Field(..., description="Public URL of the target template/scene image")


class ToolOut(BaseModel):
    result_url: str
    cost_paise: int


class RestoreIn(BaseModel):
    photo_key: str = Field(..., description="R2 key of the photo to restore (from /uploads/photo)")


class BgRemoveIn(BaseModel):
    photo_key: str = Field(..., description="R2 key of the photo (from /uploads/photo)")


class UpscaleIn(BaseModel):
    photo_key: str = Field(..., description="R2 key of the photo to upscale")
    scale: int = Field(default=4, ge=2, le=4)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _get_storage():
    from app.core.config import get_settings
    from app.adapters.storage import S3StorageAdapter, FakeStorageAdapter
    s = get_settings()
    if s.s3_endpoint_url and s.s3_access_key_id:
        return S3StorageAdapter(
            endpoint_url=str(s.s3_endpoint_url),
            access_key_id=s.s3_access_key_id,
            secret_access_key=s.s3_secret_access_key,
            bucket_name=s.s3_bucket_name,
            region=s.s3_region,
        )
    return FakeStorageAdapter()


def _presign(storage, key: str) -> str:
    from app.adapters.storage import S3StorageAdapter
    if isinstance(storage, S3StorageAdapter):
        return storage.presign(key, expires_in=3600)
    return f"fake://{key}"


async def _charge(
    db: AsyncSession,
    user: User,
    cost_paise: int,
    tool_name: str,
    idempotency_key: str,
) -> None:
    """Deduct credits. Raises 402 on insufficient balance."""
    svc = CreditsService(db)
    try:
        await svc.debit(
            user_id=user.id,
            delta_paise=cost_paise,
            reason=LedgerReason.spend,
            ref=LedgerRef(ref_type="tool", ref_id=0),
            idempotency_key=idempotency_key,
        )
        await db.commit()
    except InsufficientBalanceError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": "insufficient_balance",
                "available_paise": exc.available,
                "required_paise": exc.requested,
            },
        )


async def _upload_result(storage, user_id: int, tool_name: str, data: bytes, content_type: str) -> str:
    key = f"tool-results/{user_id}/{tool_name}/{uuid.uuid4()}.png"
    await storage.upload(key, data, content_type=content_type)
    return key


# ---------------------------------------------------------------------------
# Face Swap
# ---------------------------------------------------------------------------


@router.post("/face-swap", response_model=ToolOut)
async def face_swap(
    body: FaceSwapIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolOut:
    """Swap the user's face into a template or scene image."""
    from app.core.config import get_settings
    settings = get_settings()

    if not settings.gen_provider_api_key:
        raise HTTPException(status_code=503, detail="Face swap provider not configured")

    idem_key = f"tool:face-swap:{current_user.id}:{body.source_photo_key}:{body.target_image_url[:64]}"
    await _charge(db, current_user, _COST_FACE_SWAP, "face-swap", idem_key)

    storage = _get_storage()
    source_url = _presign(storage, body.source_photo_key)

    from app.adapters.face_swap import FalFaceSwapAdapter
    adapter = FalFaceSwapAdapter(
        api_key=settings.gen_provider_api_key,
        cost_paise=_COST_FACE_SWAP,
    )
    try:
        output = await adapter.swap(
            source_image_url=source_url,
            target_image_url=body.target_image_url,
        )
    except Exception:
        logger.exception("face-swap failed for user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="Face swap failed — please try again")

    from app.workers.pipeline import _apply_watermark
    import asyncio
    watermarked = await asyncio.to_thread(_apply_watermark, output.media_bytes)

    key = await _upload_result(storage, current_user.id, "face-swap", watermarked, "image/png")
    return ToolOut(result_url=_presign(storage, key), cost_paise=_COST_FACE_SWAP)


# ---------------------------------------------------------------------------
# Photo Restore
# ---------------------------------------------------------------------------


@router.post("/restore", response_model=ToolOut)
async def restore_photo(
    body: RestoreIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolOut:
    """Restore and enhance an old or blurry photo."""
    from app.core.config import get_settings
    settings = get_settings()

    if not settings.gen_provider_api_key:
        raise HTTPException(status_code=503, detail="Restore provider not configured")

    idem_key = f"tool:restore:{current_user.id}:{body.photo_key}"
    await _charge(db, current_user, _COST_RESTORE, "restore", idem_key)

    storage = _get_storage()
    photo_url = _presign(storage, body.photo_key)

    from app.adapters.photo_tools import PhotoRestoreAdapter
    adapter = PhotoRestoreAdapter(api_key=settings.gen_provider_api_key, cost_paise=_COST_RESTORE)
    try:
        result_bytes, _ = await adapter.restore(image_url=photo_url)
    except Exception:
        logger.exception("restore failed for user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="Photo restore failed — please try again")

    import asyncio
    from app.workers.pipeline import _apply_watermark
    watermarked = await asyncio.to_thread(_apply_watermark, result_bytes)

    key = await _upload_result(storage, current_user.id, "restore", watermarked, "image/png")
    return ToolOut(result_url=_presign(storage, key), cost_paise=_COST_RESTORE)


# ---------------------------------------------------------------------------
# Background Remove
# ---------------------------------------------------------------------------


@router.post("/bg-remove", response_model=ToolOut)
async def bg_remove(
    body: BgRemoveIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolOut:
    """Remove background from a photo. Returns transparent PNG."""
    from app.core.config import get_settings
    settings = get_settings()

    if not settings.gen_provider_api_key:
        raise HTTPException(status_code=503, detail="BG remove provider not configured")

    idem_key = f"tool:bg-remove:{current_user.id}:{body.photo_key}"
    await _charge(db, current_user, _COST_BG_REMOVE, "bg-remove", idem_key)

    storage = _get_storage()
    photo_url = _presign(storage, body.photo_key)

    from app.adapters.photo_tools import BgRemoveAdapter
    adapter = BgRemoveAdapter(api_key=settings.gen_provider_api_key, cost_paise=_COST_BG_REMOVE)
    try:
        result_bytes, _ = await adapter.remove_bg(image_url=photo_url)
    except Exception:
        logger.exception("bg-remove failed for user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="Background removal failed — please try again")

    key = await _upload_result(storage, current_user.id, "bg-remove", result_bytes, "image/png")
    return ToolOut(result_url=_presign(storage, key), cost_paise=_COST_BG_REMOVE)


# ---------------------------------------------------------------------------
# Upscale
# ---------------------------------------------------------------------------


@router.post("/upscale", response_model=ToolOut)
async def upscale_photo(
    body: UpscaleIn,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolOut:
    """Upscale a photo up to 4× using Real-ESRGAN."""
    from app.core.config import get_settings
    settings = get_settings()

    if not settings.gen_provider_api_key:
        raise HTTPException(status_code=503, detail="Upscale provider not configured")

    idem_key = f"tool:upscale:{current_user.id}:{body.photo_key}:{body.scale}"
    await _charge(db, current_user, _COST_UPSCALE, "upscale", idem_key)

    storage = _get_storage()
    photo_url = _presign(storage, body.photo_key)

    from app.adapters.photo_tools import PhotoUpscaleAdapter
    adapter = PhotoUpscaleAdapter(api_key=settings.gen_provider_api_key, cost_paise=_COST_UPSCALE)
    try:
        result_bytes, _ = await adapter.upscale(image_url=photo_url, scale=body.scale)
    except Exception:
        logger.exception("upscale failed for user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="Upscale failed — please try again")

    key = await _upload_result(storage, current_user.id, "upscale", result_bytes, "image/png")
    return ToolOut(result_url=_presign(storage, key), cost_paise=_COST_UPSCALE)
