"""
Regenerate all template cover images using OpenAI gpt-image-1.

For each template:
  1. Generate a high-quality image from scene_description via OpenAI
  2. Upload to R2 at templates/covers/{template_id}.png
  3. Update image_url in the DB to the public R2 URL

Usage:
    OPENAI_API_KEY=sk-... \\
    DATABASE_URL=postgresql+asyncpg://... \\
    S3_ENDPOINT_URL=https://... \\
    S3_ACCESS_KEY_ID=... \\
    S3_SECRET_ACCESS_KEY=... \\
    S3_BUCKET_NAME=weddingapp \\
    R2_PUBLIC_URL=https://pub-xxx.r2.dev \\
    python scripts/regenerate_template_images.py

    # Regenerate a single template:
    python scripts/regenerate_template_images.py --id 3

    # Dry-run (generate + upload but skip DB update):
    python scripts/regenerate_template_images.py --dry-run

R2_PUBLIC_URL is the public domain for your R2 bucket, e.g.:
  https://pub-abc123.r2.dev        (Cloudflare R2 public bucket)
  https://assets.yourdomain.com    (custom domain)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import sys

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config from env
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
DATABASE_URL = (
    os.environ.get("DATABASE_URL", "")
    .replace("postgresql://", "postgresql+asyncpg://", 1)
    .replace("postgres://", "postgresql+asyncpg://", 1)
)
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "")
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "weddingapp")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")


def _check_env() -> None:
    missing = [k for k in ["OPENAI_API_KEY", "DATABASE_URL", "S3_ENDPOINT_URL",
                            "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "R2_PUBLIC_URL"]
               if not os.environ.get(k)]
    if missing:
        logger.error("Missing env vars: %s", ", ".join(missing))
        sys.exit(1)


# ---------------------------------------------------------------------------
# OpenAI image generation
# ---------------------------------------------------------------------------

async def _generate_image(scene_description: str, template_name: str) -> bytes:
    """Generate a template cover image from the scene description."""
    prompt = (
        f"Create a high-quality, realistic promotional template image for an Indian events app. "
        f"Scene: {scene_description} "
        f"Style: Professional photography, vibrant colors, suitable as a template preview thumbnail. "
        f"Do NOT include any text, watermarks, or overlays. "
        f"Leave a natural empty portrait area/space in the center or foreground where a person's photo can be composited in."
    )

    logger.info("Generating image for: %s", template_name)
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": OPENAI_IMAGE_MODEL,
                "prompt": prompt,
                "n": 1,
                "size": "1024x1536",
                "quality": "high",
                "output_format": "png",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    item = data["data"][0]
    if "b64_json" in item:
        return base64.b64decode(item["b64_json"])

    # URL response — download it
    url = item["url"]
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


# ---------------------------------------------------------------------------
# R2 upload
# ---------------------------------------------------------------------------

async def _upload_to_r2(image_bytes: bytes, template_id: int) -> str:
    """Upload PNG to R2, return the public URL."""
    import boto3
    from botocore.config import Config

    key = f"templates/covers/{template_id}.png"

    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )

    await asyncio.to_thread(
        s3.put_object,
        Bucket=S3_BUCKET_NAME,
        Key=key,
        Body=image_bytes,
        ContentType="image/png",
    )

    public_url = f"{R2_PUBLIC_URL}/{key}"
    logger.info("Uploaded → %s", public_url)
    return public_url


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def regenerate(only_id: int | None = None, dry_run: bool = False) -> None:
    _check_env()

    engine = create_async_engine(DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        if only_id:
            rows = await session.execute(
                text("SELECT id, name, scene_description FROM templates WHERE id = :id AND active = true"),
                {"id": only_id},
            )
        else:
            rows = await session.execute(
                text("SELECT id, name, scene_description FROM templates WHERE active = true ORDER BY id")
            )
        templates = rows.fetchall()

    if not templates:
        logger.error("No templates found")
        return

    logger.info("Regenerating %d template image(s)…", len(templates))

    async with factory() as session:
        for tmpl_id, name, scene_desc in templates:
            if not scene_desc:
                logger.warning("Template %d (%s) has no scene_description — skipping", tmpl_id, name)
                continue

            try:
                image_bytes = await _generate_image(scene_desc, name)
                public_url = await _upload_to_r2(image_bytes, tmpl_id)

                if dry_run:
                    logger.info("[DRY RUN] Would update template %d image_url → %s", tmpl_id, public_url)
                else:
                    await session.execute(
                        text("UPDATE templates SET image_url = :url WHERE id = :id"),
                        {"url": public_url, "id": tmpl_id},
                    )
                    await session.commit()
                    logger.info("Updated template %d (%s) → %s", tmpl_id, name, public_url)

            except Exception:
                logger.exception("Failed for template %d (%s) — skipping", tmpl_id, name)
                continue

    await engine.dispose()
    logger.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Regenerate template cover images via OpenAI")
    parser.add_argument("--id", type=int, default=None, help="Regenerate only this template ID")
    parser.add_argument("--dry-run", action="store_true", help="Generate + upload but skip DB update")
    args = parser.parse_args()
    asyncio.run(regenerate(only_id=args.id, dry_run=args.dry_run))
