from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.models.user import User

router = APIRouter(prefix="/uploads", tags=["uploads"])

_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic"}
_MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


class PhotoUploadOut(BaseModel):
    photo_key: str
    photo_url: str  # presigned GET URL valid for 1 hour


@router.post(
    "/photo",
    response_model=PhotoUploadOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a user photo for use in order generation",
)
async def upload_photo(
    file: UploadFile,
    current_user: Annotated[User, Depends(get_current_user)],
) -> PhotoUploadOut:
    """
    Upload a user photo (selfie / portrait) to R2.
    Returns a photo_key that should be passed in CreateOrderIn.user_photo_key.
    The presigned photo_url is for client-side preview only (1-hour TTL).
    """
    if file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type {file.content_type!r}. Use JPEG, PNG, or WebP.",
        )

    data = await file.read()
    if len(data) > _MAX_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large ({len(data) // 1024} KB). Maximum is 10 MB.",
        )

    from app.core.config import get_settings
    from app.adapters.storage import S3StorageAdapter, FakeStorageAdapter

    settings = get_settings()
    ext = _ext_for_content_type(file.content_type)
    key = f"user-photos/{current_user.id}/{uuid.uuid4().hex}.{ext}"

    use_real = all([settings.s3_endpoint_url, settings.s3_access_key_id, settings.s3_secret_access_key])
    if use_real:
        storage = S3StorageAdapter(
            endpoint_url=str(settings.s3_endpoint_url),
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            bucket_name=settings.s3_bucket_name,
            region=settings.s3_region,
        )
        await storage.upload(key, data, content_type=file.content_type or "image/jpeg")
        photo_url = storage.presign(key, expires_in=3600)
    else:
        storage = FakeStorageAdapter()
        await storage.upload(key, data, content_type=file.content_type or "image/jpeg")
        photo_url = f"http://localhost/fake/{key}"

    return PhotoUploadOut(photo_key=key, photo_url=photo_url)


def _ext_for_content_type(ct: str | None) -> str:
    return {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic"}.get(ct or "", "jpg")
