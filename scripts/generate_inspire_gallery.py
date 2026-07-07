"""
Generate the AI styling inspiration gallery for the Inspire feature.

Instead of sourcing real celebrity/editorial photos (publicity-rights and
licensing risk — see project notes), every image here is originally
generated via OpenAI gpt-image-2, uploaded to R2, and served from
GET /inspire/styled. Safe to re-run — only inserts prompts not already
present for a category.

Usage:
    DATABASE_URL=postgresql://... \
    OPENAI_API_KEY=sk-... \
    S3_ENDPOINT_URL=... S3_ACCESS_KEY_ID=... S3_SECRET_ACCESS_KEY=... \
    python scripts/generate_inspire_gallery.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = (
    os.environ.get("DATABASE_URL", "")
    .replace("postgresql://", "postgresql+asyncpg://", 1)
    .replace("postgres://", "postgresql+asyncpg://", 1)
)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")

S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "")
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "weddingapp")
R2_PUBLIC_BASE = os.environ.get("R2_PUBLIC_BASE", "").rstrip("/")

_QUALITY_SUFFIX = (
    " Editorial fashion photography, ultra-realistic, 8K, professional studio "
    "or event lighting, magazine quality, no text, no watermark, no logos."
)

# 6 prompts per category — no real people, no celebrity names, purely
# descriptive styling scenes to avoid any publicity-rights ambiguity.
PROMPTS: dict[str, list[str]] = {
    "celebrity-styling": [
        "A red carpet event gown in shimmering sequins, dramatic pose, flashbulb lighting.",
        "A tailored designer tuxedo at a film awards night, confident stance, step-and-repeat backdrop.",
        "An elegant off-shoulder evening gown with a thigh-high slit, glamorous award-show styling.",
        "A sharp velvet blazer with a bow tie, red carpet menswear styling, dramatic spotlight.",
        "A bold metallic cocktail dress with statement jewelry, premiere-night styling.",
        "A classic black-tie gown with a dramatic train, red carpet entrance pose.",
    ],
    "billionaire-styling": [
        "A tailored charcoal three-piece suit, luxury penthouse backdrop, understated wealth aesthetic.",
        "An elegant silk evening dress paired with fine diamond jewelry, private yacht deck backdrop.",
        "A crisp white linen suit, private jet backdrop, minimalist luxury styling.",
        "A cashmere overcoat over a turtleneck, modern art gallery backdrop, quiet-luxury aesthetic.",
        "A floor-length silk gown with a fur stole, grand mansion staircase backdrop.",
        "A tailored double-breasted suit with a pocket square, luxury car showroom backdrop.",
    ],
    "actress-dressing": [
        "A flowing pastel chiffon gown, soft studio lighting, editorial fashion shoot styling.",
        "A structured power suit in bold color, confident magazine-cover pose.",
        "A romantic lace gown with delicate embroidery, golden-hour outdoor editorial shoot.",
        "A sleek satin slip dress, minimalist backdrop, high-fashion editorial styling.",
        "An elegant Anarkali-style gown with intricate embroidery, palace-courtyard backdrop.",
        "A modern draped saree gown fusion, contemporary studio fashion shoot.",
    ],
    "wedding-dress": [
        "A classic ballgown wedding dress with a cathedral veil, soft romantic lighting.",
        "A modern minimalist silk wedding gown, clean lines, elegant bridal editorial.",
        "An intricately embroidered lehenga bridal outfit, rich reds and golds, traditional Indian wedding styling.",
        "A bohemian lace wedding dress with flowing sleeves, garden backdrop, soft daylight.",
        "A regal royal-style bridal gown with a long train, palace backdrop, dramatic lighting.",
        "A sleek mermaid-fit wedding gown, elegant studio bridal editorial.",
    ],
    "reception-dress": [
        "A sequined reception gown in deep emerald, evening event lighting.",
        "A pastel tulle reception dress, soft romantic evening backdrop.",
        "A gold-embroidered reception lehenga, festive celebratory styling.",
        "A sleek satin reception gown in wine red, elegant evening party backdrop.",
        "A modern indo-western reception outfit, contemporary fusion styling.",
        "A shimmering silver reception gown, dramatic evening event lighting.",
    ],
    "mens-grooming": [
        "A modern fade haircut with a neat beard trim, confident studio portrait.",
        "A classic pompadour hairstyle, sharp grooming, editorial menswear portrait.",
        "A textured crop haircut with clean skin styling, natural daylight portrait.",
        "A slicked-back hairstyle with a groomed beard, formal menswear editorial.",
        "A short crew cut with a clean shave, athletic grooming style portrait.",
        "A modern quiff hairstyle, well-groomed look, contemporary studio portrait.",
    ],
    "womens-fashion": [
        "A flowing floral summer dress, soft natural outdoor lighting, lifestyle fashion editorial.",
        "A tailored blazer and trouser set, confident urban street style editorial.",
        "An elegant silk kurta set with delicate embroidery, warm natural lighting.",
        "A chic denim-on-denim casual outfit, urban city backdrop, lifestyle editorial.",
        "A flowy bohemian maxi dress, golden-hour beach backdrop, lifestyle fashion shoot.",
        "A structured little black dress, minimalist studio fashion editorial.",
    ],
    "new-outfit-ideas": [
        "A pastel co-ord set, fresh spring styling, soft natural lighting editorial.",
        "A monochrome streetwear outfit, urban city backdrop, contemporary fashion shoot.",
        "A relaxed linen shirt-and-trouser combo, warm daylight lifestyle editorial.",
        "A festive ethnic fusion outfit, vibrant colors, celebratory styling.",
        "A minimalist neutral-tone outfit, clean studio backdrop, modern fashion editorial.",
        "A vibrant printed outfit with statement accessories, bold fashion editorial styling.",
    ],
}


async def generate_gallery() -> None:
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not set — aborting")
        return
    if not all([S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY]):
        logger.error("S3/R2 credentials not fully set — aborting")
        return

    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.adapters.openai_image import OpenAIImageAdapter
    from app.adapters.storage import S3StorageAdapter

    adapter = OpenAIImageAdapter(api_key=OPENAI_API_KEY, model=OPENAI_MODEL, cost_paise=0)
    storage = S3StorageAdapter(
        endpoint_url=S3_ENDPOINT_URL,
        access_key_id=S3_ACCESS_KEY_ID,
        secret_access_key=S3_SECRET_ACCESS_KEY,
        bucket_name=S3_BUCKET_NAME,
    )

    engine = create_async_engine(DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    inserted = 0
    async with factory() as session:
        existing = await session.execute(text("SELECT category, prompt FROM inspire_gallery"))
        existing_pairs = {(row[0], row[1]) for row in existing.fetchall()}

        for category, prompts in PROMPTS.items():
            for prompt in prompts:
                if (category, prompt) in existing_pairs:
                    logger.info("Skip (already exists): %s / %s…", category, prompt[:40])
                    continue

                full_prompt = prompt + _QUALITY_SUFFIX
                try:
                    logger.info("Generating: %s / %s…", category, prompt[:50])
                    image_bytes, _ = await adapter.generate(full_prompt, aspect_ratio="9:16")

                    key = f"inspire-gallery/{category}/{uuid.uuid4().hex[:12]}.png"
                    await storage.upload(key, image_bytes, content_type="image/png")

                    if R2_PUBLIC_BASE:
                        image_url = f"{R2_PUBLIC_BASE}/{key}"
                    else:
                        image_url = storage.presign(key, expires_in=60 * 60 * 24 * 365)

                    await session.execute(
                        text(
                            "INSERT INTO inspire_gallery (category, prompt, image_url, active) "
                            "VALUES (:category, :prompt, :image_url, true)"
                        ),
                        {"category": category, "prompt": prompt, "image_url": image_url},
                    )
                    await session.commit()
                    inserted += 1
                    logger.info("  -> stored: %s", image_url)

                except Exception:
                    logger.exception("Failed to generate %s / %s", category, prompt[:40])

    await engine.dispose()
    logger.info("Done. Inserted %d new gallery images.", inserted)


if __name__ == "__main__":
    asyncio.run(generate_gallery())
