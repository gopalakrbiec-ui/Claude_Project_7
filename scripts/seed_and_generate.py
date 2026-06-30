"""
Reset templates and generate high-quality AI reference images for each.

Steps:
  1. Delete all existing templates (cascade-deletes nothing — orders keep their template_id FK)
  2. Insert new templates defined in TEMPLATES below
  3. For each template, generate a 768x1344 face-free reference image via fal-ai/flux/dev
  4. Upload to R2 public bucket and update templates.image_url

Run from Railway API console:
    python scripts/seed_and_generate.py

Required env vars:
    DATABASE_URL, FAL_API_KEY, S3_ENDPOINT_URL, S3_ACCESS_KEY_ID,
    S3_SECRET_ACCESS_KEY, S3_BUCKET_NAME, R2_PUBLIC_BASE
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = (
    os.environ["DATABASE_URL"]
    .replace("postgresql://", "postgresql+asyncpg://", 1)
    .replace("postgres://", "postgresql+asyncpg://", 1)
)

FAL_API_KEY = os.environ.get("FAL_API_KEY") or os.environ.get("GEN_PROVIDER_API_KEY", "")
os.environ["FAL_KEY"] = FAL_API_KEY

S3_ENDPOINT   = os.environ.get("S3_ENDPOINT_URL", "")
S3_KEY        = os.environ.get("S3_ACCESS_KEY_ID", "")
S3_SECRET     = os.environ.get("S3_SECRET_ACCESS_KEY", "")
S3_BUCKET     = os.environ.get("S3_BUCKET_NAME", "weddingapp")
S3_REGION     = os.environ.get("S3_REGION", "auto")
R2_PUBLIC_BASE = os.environ.get("R2_PUBLIC_BASE", "https://pub-a4fafa5a7fa94188b454190c60de3862.r2.dev").rstrip("/")

# ---------------------------------------------------------------------------
# TEMPLATES — fill this in with the 5 themes x 5 variants each
# Each entry: name, category, theme, scene_description, image_prompt
#   image_prompt  — detailed face-free prompt for fal-ai/flux/dev
#   scene_description — fed to Claude when building the generation prompt
# ---------------------------------------------------------------------------

TEMPLATES: list[dict] = [
    # ── THEME 1 ─────────────────────────────────────────────────────────────
    # (filled in by the user)
]

# ---------------------------------------------------------------------------
# Generation helpers
# ---------------------------------------------------------------------------

def _s3_client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_KEY,
        aws_secret_access_key=S3_SECRET,
        region_name=S3_REGION,
    )


async def _generate_image(prompt: str) -> bytes:
    import fal_client
    import httpx

    logger.info("  Generating image...")
    result = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: fal_client.run(
            "fal-ai/flux/dev",
            arguments={
                "prompt": prompt,
                "image_size": {"width": 768, "height": 1344},
                "num_inference_steps": 35,
                "guidance_scale": 3.5,
                "num_images": 1,
                "enable_safety_checker": False,
            },
        ),
    )
    images = result.get("images") or []
    if not images:
        raise RuntimeError(f"No images in fal result: {result}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(images[0]["url"])
        resp.raise_for_status()
        return resp.content


def _upload(s3, image_bytes: bytes, key: str) -> str:
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=image_bytes,
        ContentType="image/png",
        ACL="public-read",
    )
    return f"{R2_PUBLIC_BASE}/{key}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    if not TEMPLATES:
        logger.error("TEMPLATES list is empty — add your 5 themes first")
        return

    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    s3 = _s3_client()

    async with factory() as session:
        # Step 1: Remove all existing templates
        logger.info("Deleting all existing templates...")
        await session.execute(text("DELETE FROM templates"))
        await session.execute(text("ALTER SEQUENCE templates_id_seq RESTART WITH 1"))
        await session.commit()
        logger.info("Templates cleared.")

        # Step 2: Insert new templates + generate images
        for i, tmpl in enumerate(TEMPLATES, start=1):
            name            = tmpl["name"]
            category        = tmpl["category"]
            theme           = tmpl.get("theme", category)
            scene_desc      = tmpl["scene_description"]
            image_prompt    = tmpl["image_prompt"]
            base_price      = tmpl.get("base_price_paise", 2500)
            is_featured     = tmpl.get("is_featured", False)

            logger.info("[%d/%d] %s — %s", i, len(TEMPLATES), category, name)

            try:
                image_bytes = await _generate_image(image_prompt)
            except Exception as exc:
                logger.error("  Image generation failed: %s", exc)
                image_bytes = None

            image_url = ""
            if image_bytes:
                key = f"template-images/{i}-{uuid.uuid4().hex[:8]}.png"
                image_url = _upload(s3, image_bytes, key)
                logger.info("  Uploaded: %s", image_url)
            else:
                logger.warning("  No image — template will show placeholder")

            await session.execute(text("""
                INSERT INTO templates
                    (name, category, theme, scene_description, image_url,
                     base_price_paise, is_featured, active, asset_keys)
                VALUES
                    (:name, :category, :theme, :scene_description, :image_url,
                     :base_price_paise, :is_featured, true, '{}')
            """), {
                "name": name,
                "category": category,
                "theme": theme,
                "scene_description": scene_desc,
                "image_url": image_url,
                "base_price_paise": base_price,
                "is_featured": is_featured,
            })
            await session.commit()
            logger.info("  Saved to DB.")

    await engine.dispose()
    logger.info("Done — %d templates created.", len(TEMPLATES))


if __name__ == "__main__":
    asyncio.run(main())
