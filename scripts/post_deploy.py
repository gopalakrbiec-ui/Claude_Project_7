"""
Post-deploy hook — runs automatically on every Railway deploy before traffic starts.

Tasks:
  1. Run Alembic migrations (alembic upgrade head)
  2. Fix any template image_url that still points to the private R2 endpoint
     → rewrite to the public pub-*.r2.dev domain (R2_PUBLIC_BASE env var)
  3. Generate AI reference images for templates that still have placeholder URLs
     (only when FAL_API_KEY + S3 creds are present; skipped in stub/dev mode)

Safe to run multiple times — each step is idempotent.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import uuid

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db_url() -> str:
    raw = os.environ.get("DATABASE_URL", "")
    return (
        raw.replace("postgresql://", "postgresql+asyncpg://", 1)
           .replace("postgres://", "postgresql+asyncpg://", 1)
    )


async def _fix_template_urls(session_factory, public_base: str) -> None:
    """Rewrite template image_urls from private R2 endpoint to public CDN domain."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    async with session_factory() as session:
        rows = await session.execute(
            text("SELECT id, image_url FROM templates WHERE image_url LIKE '%template-images%'")
        )
        templates = rows.fetchall()
        updated = 0
        for tmpl_id, image_url in templates:
            # Already on the public domain — skip
            if image_url and image_url.startswith(public_base):
                continue
            if image_url and "template-images/" in image_url:
                path = "template-images/" + image_url.split("template-images/")[1]
                new_url = f"{public_base}/{path}"
                await session.execute(
                    text("UPDATE templates SET image_url = :url WHERE id = :id"),
                    {"url": new_url, "id": tmpl_id},
                )
                logger.info("  URL fix: template %s → %s", tmpl_id, new_url)
                updated += 1
        await session.commit()
    logger.info("URL fix: updated %d template URLs", updated)


async def _generate_missing_images(session_factory, public_base: str) -> None:
    """Generate AI images for templates still using placeholder/Unsplash URLs."""
    fal_key = os.environ.get("FAL_API_KEY") or os.environ.get("GEN_PROVIDER_API_KEY", "")
    s3_endpoint = os.environ.get("S3_ENDPOINT_URL", "")
    s3_key = os.environ.get("S3_ACCESS_KEY_ID", "")
    s3_secret = os.environ.get("S3_SECRET_ACCESS_KEY", "")
    s3_bucket = os.environ.get("S3_BUCKET_NAME", "weddingapp")

    if not all([fal_key, s3_endpoint, s3_key, s3_secret, public_base]):
        logger.info("Image generation skipped — FAL_API_KEY / S3 / R2_PUBLIC_BASE not fully configured")
        return

    # Import here so the script starts even if fal_client isn't installed
    try:
        import fal_client  # noqa: F401
        import boto3
    except ImportError:
        logger.warning("Image generation skipped — fal_client or boto3 not installed")
        return

    import fal_client
    import httpx
    import boto3
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    os.environ["FAL_KEY"] = fal_key

    # Placeholder patterns that need replacing
    PLACEHOLDER_PATTERNS = ("unsplash.com", "placeholder", "via.placeholder", "picsum")

    async with session_factory() as session:
        rows = await session.execute(
            text("SELECT id, name, category FROM templates WHERE active = true ORDER BY id")
        )
        templates = rows.fetchall()

    # Template prompts — face-free portrait references for InstantID
    TEMPLATE_PROMPTS: dict[str, str] = {
        # Wedding
        "Wedding Couple Portrait": "elegant Indian wedding backdrop, rich golden mandap with marigold garlands, warm candlelight, no people, ornate architecture",
        "Traditional Mehndi Ceremony": "vibrant mehndi ceremony setting, yellow and orange marigold decorations, dholak drum, henna patterns on cloth, no people",
        "Engagement Ceremony": "beautiful engagement ceremony backdrop, rose petals scattered, golden rings on velvet cushion, fairy lights, no people",
        "Wedding Reception": "grand wedding reception hall, crystal chandeliers, round tables with white linens and rose centerpieces, no people",
        # Bollywood
        "Bollywood Diva": "glamorous Bollywood movie poster backdrop, vibrant neon lights, film studio set, golden award statuette, dramatic spotlight, no people",
        "Bollywood Hero": "Bollywood action hero movie poster, dramatic skyline backdrop, motorcycle silhouette, intense lighting, no people",
        "Retro Bollywood": "vintage 70s Bollywood aesthetic, warm sepia tones, film grain, old Hindi movie poster border art, no people",
        # Cricket
        "Cricket Glory": "cricket stadium at night with floodlights, crowd in stands blurred, pitch wickets, Indian flag waving, no people",
        "World Cup Champion": "cricket World Cup trophy on a pedestal, confetti falling, stadium background, golden light, no people",
        "IPL Star": "IPL cricket stadium backdrop, team jersey hanging, colorful IPL logo, cheerful stadium atmosphere, no people",
        # Royal
        "Royal India Maharaja": "opulent Indian palace interior, ornate marble pillars, peacock motifs, golden throne, rich silk drapes, no people",
        "Rajasthani Princess": "Rajasthani haveli backdrop, intricate jharokha window, peacock blue and gold colors, desert dunes at sunset, no people",
        "Mughal Emperor": "Mughal architecture backdrop, red sandstone fort, intricate geometric patterns, lush garden with fountains, no people",
        # Birthday
        "Kids Birthday Party": "colorful children birthday party scene, balloons streamers confetti, cartoon cake, rainbow decorations, no people",
        "Milestone Birthday (50/60/75)": "elegant milestone birthday celebration, golden 50 balloon numbers, champagne glasses, sophisticated decoration, no people",
        "Birthday Bash": "fun birthday party backdrop, disco ball, neon lights, birthday cake with sparklers, festive atmosphere, no people",
        "Birthday Wishes": "warm birthday celebration setting, candles on cake, flower garlands, greeting card aesthetic, soft bokeh, no people",
        # Professional
        "Business Suit Portrait": "modern corporate office backdrop, city skyline through floor-to-ceiling glass windows, professional desk setup, no people",
        "LinkedIn Pro": "professional headshot backdrop, clean modern office with plants, neutral tones, corporate aesthetic, no people",
        "Traditional Kurta Look": "elegant traditional Indian study room, wooden bookshelf with brass artifacts, warm ambient lighting, no people",
        "Saree Elegance": "luxurious Indian fashion studio backdrop, silk fabric draped aesthetically, soft studio lighting, no people",
        # Festival
        "Diwali Celebration": "Diwali festival backdrop, rows of diyas glowing, rangoli pattern, sparklers light trails, no people",
        "Holi Festival": "Holi festival scene, colorful powder clouds, water balloons, bright spring setting, no people",
        "Navratri": "Navratri garba celebration, dandiya sticks, colorful chaniya choli fabric, temple backdrop, no people",
        "Eid Mubarak": "Eid celebration backdrop, crescent moon and stars, lanterns, mosque silhouette at sunset, no people",
        # Fashion
        "Street Style": "urban street fashion backdrop, graffiti wall, city sidewalk, cool urban aesthetic, no people",
        "Ethnic Fashion": "traditional Indian fashion backdrop, embroidered fabric textures, jewelry display, warm studio lighting, no people",
        # Family
        "Family Portrait": "warm family living room setting, comfortable sofa, family photo frames on wall, soft natural light, no people",
        "Grandparents Day": "cozy home setting, rocking chairs in garden, flowering plants, warm golden hour light, no people",
    }

    s3 = boto3.client(
        "s3",
        endpoint_url=s3_endpoint,
        aws_access_key_id=s3_key,
        aws_secret_access_key=s3_secret,
        region_name=os.environ.get("S3_REGION", "auto"),
    )

    async with session_factory() as session:
        for tmpl_id, name, category in templates:
            rows2 = await session.execute(
                text("SELECT image_url FROM templates WHERE id = :id"),
                {"id": tmpl_id},
            )
            current_url = rows2.scalar()

            # Skip if already on public R2
            if current_url and current_url.startswith(public_base):
                logger.info("Skip template %d (%s) — already has R2 image", tmpl_id, name)
                continue

            # Skip if not a placeholder
            if current_url and not any(p in current_url for p in PLACEHOLDER_PATTERNS):
                logger.info("Skip template %d (%s) — custom image set", tmpl_id, name)
                continue

            prompt = TEMPLATE_PROMPTS.get(name)
            if not prompt:
                logger.warning("No prompt for template %d (%s) — skipping", tmpl_id, name)
                continue

            logger.info("Generating image for template %d: %s", tmpl_id, name)
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda p=prompt: fal_client.run(
                        "fal-ai/flux/dev",
                        arguments={
                            "prompt": p,
                            "image_size": {"width": 768, "height": 1024},
                            "num_inference_steps": 28,
                            "guidance_scale": 3.5,
                            "num_images": 1,
                            "enable_safety_checker": True,
                        },
                    ),
                )
                images = result.get("images") or []
                if not images:
                    logger.error("No images returned for template %d", tmpl_id)
                    continue

                image_url_fal = images[0].get("url") or images[0].get("b64_json")
                async with httpx.AsyncClient(timeout=60.0) as client:
                    img_resp = await client.get(image_url_fal)
                    img_resp.raise_for_status()
                    image_bytes = img_resp.content

                key = f"template-images/{tmpl_id}-{uuid.uuid4().hex[:8]}.png"
                s3.put_object(
                    Bucket=s3_bucket,
                    Key=key,
                    Body=image_bytes,
                    ContentType="image/png",
                    ACL="public-read",
                )
                new_url = f"{public_base}/{key}"
                await session.execute(
                    text("UPDATE templates SET image_url = :url WHERE id = :id"),
                    {"url": new_url, "id": tmpl_id},
                )
                await session.commit()
                logger.info("  Generated + uploaded: %s", new_url)

            except Exception as exc:
                logger.error("Failed to generate image for template %d (%s): %s", tmpl_id, name, exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    db_url = _db_url()
    if not db_url.startswith("postgresql"):
        logger.error("DATABASE_URL not set or invalid — skipping post-deploy hooks")
        return

    engine = create_async_engine(db_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Fallback to the known R2 public domain if env var not set
    public_base = (
        os.environ.get("R2_PUBLIC_BASE", "")
        or "https://pub-a4fafa5a7fa94188b454190c60de3862.r2.dev"
    ).rstrip("/")

    # Step 1: Fix private → public URL for already-uploaded images
    logger.info("=== Step 1: Fix template image URLs to public R2 domain ===")
    await _fix_template_urls(session_factory, public_base)

    # Step 2: Generate missing template images
    logger.info("=== Step 2: Generate missing template images ===")
    await _generate_missing_images(session_factory, public_base)

    await engine.dispose()
    logger.info("=== Post-deploy hooks complete ===")


if __name__ == "__main__":
    asyncio.run(main())
