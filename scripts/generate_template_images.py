"""
Generate high-quality AI reference images for every template and upload to R2.

Each image is generated with fal-ai/flux/dev (highest quality Flux model):
  - Face-free — no specific person, just pose/outfit/scene/lighting
  - Portrait orientation 768×1024 — ideal for InstantID style reference
  - Detailed cinematic prompts tuned per category

After generation, updates templates.image_url in the DB.

Usage (run from Railway API console or locally with env vars set):
    python scripts/generate_template_images.py

Required env vars:
    DATABASE_URL         — PostgreSQL connection string
    FAL_API_KEY          — fal.ai API key (same as GEN_PROVIDER_API_KEY)
    S3_ENDPOINT_URL      — R2 endpoint
    S3_ACCESS_KEY_ID     — R2 key
    S3_SECRET_ACCESS_KEY — R2 secret
    S3_BUCKET_NAME       — R2 bucket
"""
from __future__ import annotations

import asyncio
import os
import uuid
import logging

import fal_client
import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATABASE_URL = (
    os.environ["DATABASE_URL"]
    .replace("postgresql://", "postgresql+asyncpg://", 1)
    .replace("postgres://", "postgresql+asyncpg://", 1)
)

FAL_API_KEY = os.environ.get("FAL_API_KEY") or os.environ.get("GEN_PROVIDER_API_KEY", "")
os.environ["FAL_KEY"] = FAL_API_KEY

S3_ENDPOINT = os.environ.get("S3_ENDPOINT_URL", "")
S3_KEY_ID   = os.environ.get("S3_ACCESS_KEY_ID", "")
S3_SECRET   = os.environ.get("S3_SECRET_ACCESS_KEY", "")
S3_BUCKET   = os.environ.get("S3_BUCKET_NAME", "weddingapp")

_MODEL = "fal-ai/flux/dev"
_W, _H  = 768, 1024   # portrait — best for InstantID style reference

# ---------------------------------------------------------------------------
# Image prompt per template name
# These are face-free scene/outfit/lighting descriptions.
# InstantID will graft the user's face onto the generated pose.
# ---------------------------------------------------------------------------

TEMPLATE_PROMPTS: dict[str, str] = {

    # ── Wedding ──────────────────────────────────────────────────────────────
    "Floral Mandap": (
        "A person in traditional Indian bridal attire standing under a grand floral mandap "
        "decorated with fresh marigold garlands, red roses, and flickering oil diyas. "
        "Warm golden hour light, shallow depth of field, ultra-realistic 8K DSLR photography. "
        "Face not visible — focus on outfit, mandap, and atmosphere."
    ),
    "Royal Sherwani": (
        "A person wearing a richly embroidered deep red and gold sherwani with a matching safa turban "
        "and pearl necklace, standing in front of a Rajasthani palace sandstone archway. "
        "Regal cinematic lighting, 8K DSLR portrait, elegant bokeh background. "
        "Silhouette pose, face angled away — showcase the outfit and palace backdrop."
    ),
    "Bridal Lehenga": (
        "A bride wearing an ornate crimson and gold bridal lehenga with heavy Kundan jewelry, "
        "maang tikka, and kalire, standing in a lush rose garden at golden hour. "
        "Professional wedding photography, 8K, cinematic colour grade. "
        "Side profile pose emphasising the lehenga and jewellery."
    ),
    "Garden Wedding": (
        "Two people in elegant wedding attire standing under a grand floral arch of white roses "
        "and trailing greenery in a sunlit garden. Soft natural light, 8K DSLR wedding portrait. "
        "Viewed from behind — focus on the arch and outfit details."
    ),
    "Royal Couple Portrait": (
        "Two people in matching royal Indian wedding outfits — bride in a pink lehenga, "
        "groom in a cream sherwani — seated on an ornate gold throne in a palace hall. "
        "Dramatic Mughal-era lighting, 8K cinematic portrait. Faces turned slightly away."
    ),

    # ── Birthday ─────────────────────────────────────────────────────────────
    "Grand Birthday": (
        "A person in festive party attire surrounded by golden confetti, shimmering balloons, "
        "and a five-tier cake with sparklers. Warm celebration lighting, 8K DSLR portrait. "
        "Joyful pose, hands raised, face not clearly visible — focus on the party atmosphere."
    ),
    "Kids Birthday": (
        "A child in a colourful party outfit with a birthday crown, surrounded by cartoon balloons, "
        "streamers, and a rainbow-layered cake. Bright cheerful lighting, 8K DSLR. "
        "Candid joy, back to camera revealing the decorated party hall."
    ),
    "Milestone 18th": (
        "A young adult in stylish semi-formal party wear standing in front of a neon '18' sign "
        "with confetti and bokeh lights. Modern party aesthetic, cinematic 8K photography. "
        "Confident pose, face partially turned."
    ),
    "Milestone 50th": (
        "A person in elegant ethnic wear seated at a beautifully decorated dining table with "
        "golden '50' balloons, flowers, and candles. Warm intimate celebration lighting, 8K DSLR."
    ),

    # ── Fashion & Portrait ───────────────────────────────────────────────────
    "Bollywood Glamour": (
        "A person in a heavily embellished sequined lehenga or Bollywood-style gown standing "
        "centre-stage with dramatic spotlights and a grand film-set backdrop. "
        "Cinematic 8K photography, vibrant colours, movie-poster composition. "
        "Dramatic full-body pose, face slightly turned from camera."
    ),
    "Street Fashion": (
        "A person in stylish urban streetwear — oversized jacket, cargo pants, designer sneakers — "
        "leaning against a colourful mural in a bustling Indian city lane. "
        "Editorial fashion photography, 8K, vibrant street energy. "
        "Cool confident pose, face angled away."
    ),
    "Traditional Saree": (
        "A person draped in an elegant Banarasi silk saree with gold zari work and traditional "
        "jewellery, standing in a haveli courtyard with carved marble pillars. "
        "Soft diffused sunlight, 8K DSLR portrait photography. "
        "Graceful three-quarter pose, gaze downward."
    ),
    "Festive Kurta": (
        "A person in a richly embroidered cream and gold kurta-pajama with a Nehru jacket, "
        "standing in a marigold-decorated courtyard during Diwali. "
        "Warm golden diyas in background, 8K DSLR photography. "
        "Relaxed festive pose, face turned toward the diyas."
    ),

    # ── Baby & Family ─────────────────────────────────────────────────────────
    "Baby Shower": (
        "A person in soft pastel maternity wear surrounded by pink and white balloons, "
        "floral decorations, and a beautiful baby shower cake. "
        "Soft natural light, 8K DSLR photography. Gentle seated pose, face slightly down."
    ),
    "Naming Ceremony": (
        "A family in traditional Indian attire holding a newborn baby at a Namkaran ceremony, "
        "surrounded by marigold garlands and flickering diyas. "
        "Warm intimate 8K DSLR portrait. Viewed from the side, faces not visible."
    ),
    "Family Portrait": (
        "A family of four in matching ethnic outfits seated on a ornate sofa in a beautifully "
        "decorated living room. Warm golden light, 8K professional DSLR photography. "
        "Viewed from a slight angle, focus on outfits and setting."
    ),

    # ── Festival ─────────────────────────────────────────────────────────────
    "Diwali Celebration": (
        "A person in rich ethnic wear crouching to light a row of oil diyas, "
        "surrounded by intricate rangoli and warm festival lights. "
        "Magical Diwali atmosphere, 8K DSLR photography. "
        "Face illuminated by diya glow, looking at the flame."
    ),
    "Housewarming (Griha Pravesh)": (
        "A family at the entrance of a new home decorated with marigold torans, "
        "a clay pot with coconut, diyas, and a rangoli. "
        "Joyful auspicious setting, 8K DSLR portrait. Viewed from behind entering the door."
    ),

    # ── Bollywood ─────────────────────────────────────────────────────────────
    "Bollywood Diva": (
        "A person in a glamorous sequined Bollywood lehenga with heavy makeup and dramatic jewellery, "
        "standing on a grand film studio staircase with spotlights and a packed crowd. "
        "Cinematic 8K photography, vibrant Bollywood colour grade, movie-poster style. "
        "Powerful arms-wide pose, face lifted upward."
    ),
    "Retro Bollywood Portrait": (
        "A person in 1970s vintage Bollywood attire — flared trousers, embroidered kurta — "
        "in a sepia-toned film-set with retro props. "
        "Film-grain texture, warm vintage tones, Filmfare magazine cover composition. "
        "Classic side profile pose."
    ),
    "Bollywood Hero": (
        "A person in a sharp designer suit or embroidered sherwani on a grand palace staircase "
        "with dramatic back-lighting and slow-motion smoke effect. "
        "Cinematic widescreen 8K photography, Bollywood action-hero movie-poster style. "
        "Arms crossed, confident stance, face slightly raised."
    ),
    "Punjabi Bride": (
        "A bride in a vibrant pink and red phulkari dupatta with heavy gold jewellery and kalire, "
        "standing in a mustard field at golden hour. "
        "8K DSLR wedding photography, Bollywood colour grade. "
        "Twirling pose, dupatta flowing in the wind, face turned to the side."
    ),

    # ── Cricket ───────────────────────────────────────────────────────────────
    "Cricket Glory": (
        "A cricketer in full Team India blue jersey raising a bat in celebration, "
        "with a packed stadium roaring in the background and confetti falling. "
        "Action 8K sports photography, dramatic floodlight stadium atmosphere. "
        "Full body celebration pose, face toward the crowd."
    ),
    "Stadium Champion": (
        "A cricketer in Team India jersey lifting a golden trophy above their head, "
        "fireworks exploding over the stadium, teammates in the background. "
        "Epic 8K sports photography, golden hour light, ultra-wide cinematic lens. "
        "Triumphant upward stretch pose."
    ),
    "Street Cricket Star": (
        "A young person in casual clothes playing cricket on a dusty street pitch "
        "with makeshift wickets and a wooden bat, afternoon sun casting long shadows. "
        "Documentary-style 8K photography, warm Indian summer light. "
        "Mid-swing batting action pose."
    ),

    # ── Royal India ───────────────────────────────────────────────────────────
    "Royal India Maharaja": (
        "A person in a silk achkan with a jewelled Rajasthani safa turban and pearl necklace, "
        "seated on an ornate gold and ruby throne in a grand palace hall. "
        "Regal oil-painting lighting, 8K, Mughal-era grandeur. "
        "Dignified seated pose, one hand on armrest, facing slightly to the side."
    ),
    "Royal Maharani": (
        "A person in a silk Banarasi saree with full Kundan jewellery set, "
        "standing in a palace zenana with intricate jali screens and marigold garlands. "
        "Royal portrait, 8K DSLR, Mughal miniature painting colour palette. "
        "Elegant standing pose, gaze downward at a rose in hand."
    ),
    "Rajput Warrior": (
        "A person in full Rajput armour with a sword and shield, "
        "standing on a fort rampart at dusk with the Aravalli hills in the background. "
        "Epic cinematic 8K photography, dramatic golden-red sky. "
        "Heroic stance, sword raised, looking into the distance."
    ),

    # ── Professional ─────────────────────────────────────────────────────────
    "LinkedIn Pro": (
        "A person in a crisp business formal outfit — navy suit or silk saree blouse — "
        "standing against a clean neutral studio background with soft professional lighting. "
        "Corporate headshot style, 8K DSLR, LinkedIn profile photo composition. "
        "Confident arms-crossed pose, slight forward lean, face with professional smile."
    ),
    "Startup Founder": (
        "A person in smart-casual blazer over a t-shirt in a modern co-working space "
        "with laptops, plants, and a whiteboard in the background. "
        "Candid 8K corporate photography, entrepreneurial energy. "
        "Relaxed leaning-on-desk pose, face engaged and confident."
    ),
    "Campus Yearbook": (
        "A person in graduation gown and cap holding a rolled diploma, "
        "smiling in a sunlit university campus with green lawns and stone buildings. "
        "Classic 8K yearbook portrait, warm celebratory atmosphere. "
        "Proud standing pose, diploma held at chest height."
    ),

    # ── Extra / alternate name variants ──────────────────────────────────────
    "Bridal Lehenga": (
        "A bride wearing an ornate crimson and gold bridal lehenga with heavy Kundan jewelry, "
        "maang tikka, and kalire, standing in a lush rose garden at golden hour. "
        "Professional wedding photography, 8K, cinematic colour grade. "
        "Side profile pose emphasising the lehenga and jewellery."
    ),
    "Kids Birthday": (
        "A child in a colourful party outfit with a birthday crown, surrounded by cartoon balloons, "
        "streamers, and a rainbow-layered cake. Bright cheerful lighting, 8K DSLR. "
        "Candid joy, back to camera revealing the decorated party hall."
    ),
    "Punjabi Bride": (
        "A bride in a vibrant pink and red phulkari dupatta with heavy gold jewellery and kalire, "
        "standing in a mustard field at golden hour. "
        "8K DSLR wedding photography, Bollywood colour grade. "
        "Twirling pose, dupatta flowing in the wind, face turned to the side."
    ),
    "Bollywood Glamour": (
        "A person in a heavily embellished sequined lehenga standing centre-stage "
        "with dramatic spotlights and a grand film-set backdrop. "
        "Cinematic 8K photography, vibrant colours, movie-poster composition. "
        "Dramatic full-body pose, face slightly turned from camera."
    ),
    "Street Fashion": (
        "A person in stylish urban streetwear leaning against a colourful mural "
        "in a bustling Indian city lane. Editorial fashion photography, 8K. "
        "Cool confident pose, face angled away."
    ),
    "Traditional Saree": (
        "A person draped in an elegant Banarasi silk saree with gold zari work, "
        "standing in a haveli courtyard with carved marble pillars. "
        "Soft diffused sunlight, 8K DSLR portrait. Graceful three-quarter pose."
    ),
    "Festive Kurta": (
        "A person in a richly embroidered cream and gold kurta-pajama with a Nehru jacket, "
        "standing in a marigold-decorated courtyard during Diwali. "
        "Warm golden diyas in background, 8K DSLR photography."
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def generate_image(prompt: str) -> bytes:
    logger.info("Generating image: %s...", prompt[:60])
    handler = await fal_client.submit_async(
        _MODEL,
        arguments={
            "prompt": prompt,
            "image_size": {"width": _W, "height": _H},
            "num_inference_steps": 28,
            "guidance_scale": 3.5,
            "num_images": 1,
            "enable_safety_checker": False,
        },
    )
    result = await handler.get()
    images = result.get("images") or []
    if not images:
        raise RuntimeError(f"No images in fal result: {result}")
    image_url: str = images[0]["url"]
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(image_url)
        resp.raise_for_status()
        return resp.content


def upload_to_r2(key: str, data: bytes) -> str:
    import boto3
    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_KEY_ID,
        aws_secret_access_key=S3_SECRET,
        region_name="auto",
    )
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=data,
        ContentType="image/png",
        # Public read so Flutter can display without presigning
        ACL="public-read",
    )
    # Return public URL
    endpoint = S3_ENDPOINT.rstrip("/")
    return f"{endpoint}/{S3_BUCKET}/{key}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    if not FAL_API_KEY:
        raise SystemExit("FAL_API_KEY / GEN_PROVIDER_API_KEY not set")
    if not S3_ENDPOINT:
        raise SystemExit("S3_ENDPOINT_URL not set")

    engine = create_async_engine(DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        # Fetch only templates that still have Unsplash/placeholder image_url
        # (skip ones already updated to R2 URLs)
        rows = await session.execute(
            text(
                "SELECT id, name FROM templates WHERE active = true "
                "AND (image_url IS NULL OR image_url LIKE '%unsplash%' OR image_url LIKE '%pexels%') "
                "ORDER BY id"
            )
        )
        templates = rows.fetchall()
        logger.info("Found %d templates to process", len(templates))

    await engine.dispose()

    results: list[tuple[int, str, str]] = []  # (id, name, new_image_url)

    for tmpl_id, tmpl_name in templates:
        prompt = TEMPLATE_PROMPTS.get(tmpl_name)
        if not prompt:
            logger.warning("No prompt defined for template '%s' (id=%s) — skipping", tmpl_name, tmpl_id)
            continue

        try:
            image_bytes = await generate_image(prompt)
            key = f"template-images/{tmpl_id}-{uuid.uuid4().hex[:8]}.png"
            image_url = upload_to_r2(key, image_bytes)
            results.append((tmpl_id, tmpl_name, image_url))
            logger.info("✓ %s → %s", tmpl_name, image_url)
        except Exception:
            logger.exception("✗ Failed for template '%s' (id=%s)", tmpl_name, tmpl_id)

        # Small delay to avoid hammering fal.ai
        await asyncio.sleep(2)

    if not results:
        logger.warning("No images generated — check errors above")
        return

    # Update DB
    engine = create_async_engine(DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        for tmpl_id, tmpl_name, image_url in results:
            await session.execute(
                text("UPDATE templates SET image_url = :url WHERE id = :id"),
                {"url": image_url, "id": tmpl_id},
            )
        await session.commit()
        logger.info("Updated image_url for %d templates", len(results))

    await engine.dispose()
    logger.info("Done. %d / %d templates updated.", len(results), len(templates))


if __name__ == "__main__":
    asyncio.run(main())
