from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class StorageAdapter(Protocol):
    async def upload(self, key: str, data: bytes, content_type: str) -> str:
        """Upload data and return the public/internal URL or key."""
        ...


class S3StorageAdapter:
    """
    Uploads objects to S3-compatible storage (Cloudflare R2 in prod, MinIO locally).
    Uses boto3 in a thread pool because boto3 is synchronous.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket_name: str,
        region: str = "auto",
    ) -> None:
        import boto3

        self._bucket = bucket_name
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
        )

    async def upload(self, key: str, data: bytes, content_type: str) -> str:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        logger.info("Uploaded %d bytes to s3://%s/%s", len(data), self._bucket, key)
        return key


class FakeStorageAdapter:
    """In-memory fake for tests. Stores uploaded bytes by key."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def upload(self, key: str, data: bytes, content_type: str) -> str:
        self.store[key] = data
        return key
