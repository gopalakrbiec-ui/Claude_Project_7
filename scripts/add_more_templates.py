"""
Additive template seed — adds new templates WITHOUT touching existing rows.

Unlike seed_templates.py (which TRUNCATEs the table for fresh dev setup),
this script only INSERTs new templates, safe to run against production —
existing templates and any orders referencing them are untouched.

Usage:
    DATABASE_URL=postgresql://... python scripts/add_more_templates.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = (
    os.environ.get("DATABASE_URL", "")
    .replace("postgresql://", "postgresql+asyncpg://", 1)
    .replace("postgres://", "postgresql+asyncpg://", 1)
)

BASE_PRICE_PAISE = 1200  # flat 10%-margin price, matches all existing templates

QUALITY_BLOCK = (
    "Ultra-realistic DSLR photography, authentic skin texture, natural facial "
    "proportions, perfect eye detail, realistic clothing folds, cinematic "
    "composition, volumetric lighting, HDR, 85mm portrait lens, f/1.8 "
    "aperture, shallow depth of field, premium color grading, professional "
    "photography, magazine quality, ultra-detailed, photorealistic, 8K resolution."
)

NEGATIVE_PROMPT = (
    "blurry, low resolution, cartoon, CGI, painting, oversaturated, "
    "watermark, logo, text, cropped face, duplicate people, extra limbs, bad "
    "anatomy, distorted hands, extra fingers, unrealistic skin, noisy image, "
    "motion blur, compression artifacts."
)


def _scene(core: str) -> str:
    return f"{core} {QUALITY_BLOCK}"


TEMPLATES = [
    # ── Childhood ──────────────────────────────────────────────────────────
    {"name": "Nostalgic School Days", "category": "childhood", "theme": "school-memory", "tags": ["childhood", "school", "nostalgia"], "aspect_ratio": "9:16",
     "scene_description": _scene("A child in a crisp school uniform, joyful expression, standing in front of a classroom chalkboard, holding books. Soft warm daylight through classroom windows.")},
    {"name": "Village Childhood", "category": "childhood", "theme": "rural", "tags": ["childhood", "village", "rural"], "aspect_ratio": "9:16",
     "scene_description": _scene("A child playing near a rural courtyard house, simple cotton clothes, big genuine smile, mud walls and clay pots in the background. Golden hour sunlight.")},
    {"name": "Playground Joy", "category": "childhood", "theme": "playground", "tags": ["childhood", "playground", "fun"], "aspect_ratio": "9:16",
     "scene_description": _scene("A child mid-laugh on a playground swing, casual bright-colored clothes, park greenery and swings in the background. Cheerful midday light.")},
    {"name": "Festive Childhood Memories", "category": "childhood", "theme": "festival", "tags": ["childhood", "festival", "traditional"], "aspect_ratio": "9:16",
     "scene_description": _scene("A child dressed in traditional festive attire, holding a diya or sparkler, standing in a decorated home courtyard with string lights. Warm festive glow lighting.")},

    # ── School ─────────────────────────────────────────────────────────────
    {"name": "Classroom Portrait", "category": "school", "theme": "classroom", "tags": ["school", "classroom", "student"], "aspect_ratio": "9:16",
     "scene_description": _scene("A school student in uniform, confident smile, seated at a wooden desk with notebooks and a globe nearby. Soft natural classroom lighting.")},
    {"name": "School Annual Day", "category": "school", "theme": "annual-day", "tags": ["school", "annual-day", "stage"], "aspect_ratio": "9:16",
     "scene_description": _scene("A student on a decorated school stage holding a trophy or certificate, proud expression, colorful backdrop with balloons. Warm stage spotlight.")},
    {"name": "School Sports Day", "category": "school", "theme": "sports", "tags": ["school", "sports", "athletic"], "aspect_ratio": "9:16",
     "scene_description": _scene("A student in sports uniform mid-action on a school running track, determined expression, cheering crowd blurred in background. Bright outdoor daylight.")},
    {"name": "School Farewell", "category": "school", "theme": "farewell", "tags": ["school", "farewell", "friends"], "aspect_ratio": "9:16",
     "scene_description": _scene("A student in formal farewell attire holding a bouquet, emotional warm smile, school garden with friends blurred in the background. Soft evening light.")},

    # ── College ────────────────────────────────────────────────────────────
    {"name": "Campus Friends", "category": "college", "theme": "campus", "tags": ["college", "friends", "campus"], "aspect_ratio": "9:16",
     "scene_description": _scene("College students in casual campus wear, laughing together, sitting on campus steps with backpacks. Natural daylight, candid composition.")},
    {"name": "College Fest", "category": "college", "theme": "fest", "tags": ["college", "fest", "celebration"], "aspect_ratio": "9:16",
     "scene_description": _scene("A college student at a vibrant campus fest, trendy outfit, colorful lights and decorations in background, energetic pose. Cinematic evening lighting.")},
    {"name": "Graduation Day", "category": "college", "theme": "graduation", "tags": ["college", "graduation", "achievement"], "aspect_ratio": "9:16",
     "scene_description": _scene("A graduate in cap and gown holding a diploma scroll, proud confident smile, university building facade in background. Soft golden hour light.")},
    {"name": "Library Study Moment", "category": "college", "theme": "study", "tags": ["college", "library", "academic"], "aspect_ratio": "9:16",
     "scene_description": _scene("A college student surrounded by books at a library desk, focused thoughtful expression, warm reading-lamp lighting, wooden shelves in background.")},

    # ── Love ───────────────────────────────────────────────────────────────
    {"name": "Romantic Candlelight Dinner", "category": "love", "theme": "candlelight", "tags": ["love", "romantic", "dinner"], "aspect_ratio": "9:16",
     "scene_description": _scene("A couple at a candlelit dinner table, elegant evening attire, warm intimate eye contact, soft bokeh string lights in background. Warm romantic lighting.")},
    {"name": "Rain Romance", "category": "love", "theme": "rain", "tags": ["love", "rain", "romantic"], "aspect_ratio": "9:16",
     "scene_description": _scene("A couple sharing an umbrella in gentle rain, casual chic clothing, wet street reflections and city lights in background. Moody cinematic rain lighting.")},
    {"name": "Sunset Beach Couple", "category": "love", "theme": "beach", "tags": ["love", "beach", "sunset"], "aspect_ratio": "9:16",
     "scene_description": _scene("A couple walking hand-in-hand on a beach at sunset, flowing casual outfits, golden waves and horizon in background. Warm golden-hour lighting.")},
    {"name": "Coffee Date Couple", "category": "love", "theme": "cafe", "tags": ["love", "cafe", "casual"], "aspect_ratio": "9:16",
     "scene_description": _scene("A couple smiling over coffee cups at a cozy cafe table, smart-casual outfits, warm string lights and cafe decor in background. Soft ambient lighting.")},

    # ── Engagement ─────────────────────────────────────────────────────────
    {"name": "Ring Ceremony", "category": "engagement", "theme": "ring-ceremony", "tags": ["engagement", "ring", "ceremony"], "aspect_ratio": "9:16",
     "scene_description": _scene("A couple exchanging rings at an engagement ceremony, elegant traditional-formal attire, floral mandap backdrop with soft fairy lights. Warm celebratory lighting.")},
    {"name": "Garden Engagement", "category": "engagement", "theme": "garden", "tags": ["engagement", "garden", "outdoor"], "aspect_ratio": "9:16",
     "scene_description": _scene("A couple embracing in a lush flower garden, pastel formal outfits, blooming floral arch in background. Soft natural daylight.")},
    {"name": "Traditional Engagement", "category": "engagement", "theme": "traditional", "tags": ["engagement", "traditional", "ceremony"], "aspect_ratio": "9:16",
     "scene_description": _scene("A couple in traditional silk attire during an engagement ritual, seated on a decorated stage with marigold garlands. Warm golden ceremonial lighting.")},
    {"name": "Sunset Proposal", "category": "engagement", "theme": "proposal", "tags": ["engagement", "proposal", "sunset"], "aspect_ratio": "9:16",
     "scene_description": _scene("A romantic proposal moment on one knee at sunset, elegant casual attire, silhouetted hills or shoreline in background. Warm dramatic sunset lighting.")},

    # ── Wedding (top-up) ───────────────────────────────────────────────────
    {"name": "Cinematic Rain Wedding", "category": "wedding", "theme": "rain", "tags": ["wedding", "rain", "cinematic"], "aspect_ratio": "9:16",
     "scene_description": _scene("A bride and groom sharing an umbrella in soft rain, elegant wedding attire, glistening street or garden background. Moody cinematic lighting.")},
    {"name": "Sunset Beach Wedding", "category": "wedding", "theme": "beach", "tags": ["wedding", "beach", "sunset"], "aspect_ratio": "9:16",
     "scene_description": _scene("A bride and groom standing on a beach at sunset, flowing wedding attire, ocean waves and golden sky in background. Warm cinematic golden-hour lighting.")},

    # ── Pregnancy ──────────────────────────────────────────────────────────
    {"name": "Maternity Garden Shoot", "category": "pregnancy", "theme": "garden", "tags": ["pregnancy", "maternity", "garden"], "aspect_ratio": "9:16",
     "scene_description": _scene("An expectant mother in a flowing maternity gown, gentle hands on belly, blooming garden background. Soft natural daylight, tender expression.")},
    {"name": "Traditional Godh Bharai", "category": "pregnancy", "theme": "godh-bharai", "tags": ["pregnancy", "traditional", "ceremony"], "aspect_ratio": "9:16",
     "scene_description": _scene("An expectant mother in traditional silk saree during a Godh Bharai ceremony, decorated mandap with flowers in background. Warm festive lighting.")},
    {"name": "Silhouette Maternity", "category": "pregnancy", "theme": "silhouette", "tags": ["pregnancy", "silhouette", "artistic"], "aspect_ratio": "9:16",
     "scene_description": _scene("A silhouette portrait of an expectant mother against a glowing sunset sky, flowing gown, serene calm pose. Warm backlit golden lighting.")},
    {"name": "Beach Maternity", "category": "pregnancy", "theme": "beach", "tags": ["pregnancy", "beach", "outdoor"], "aspect_ratio": "9:16",
     "scene_description": _scene("An expectant mother walking barefoot on a beach at golden hour, soft flowing dress, gentle waves in background. Warm natural sunset lighting.")},

    # ── Baby ───────────────────────────────────────────────────────────────
    {"name": "Newborn Sleeping Angel", "category": "baby", "theme": "newborn", "tags": ["baby", "newborn", "sleeping"], "aspect_ratio": "9:16",
     "scene_description": _scene("A newborn baby sleeping peacefully wrapped in a soft blanket, tiny hands near face, cozy neutral-tone nursery background. Soft warm studio lighting.")},
    {"name": "Baby First Birthday", "category": "baby", "theme": "birthday", "tags": ["baby", "birthday", "celebration"], "aspect_ratio": "9:16",
     "scene_description": _scene("A baby in a cute party outfit next to a small birthday cake, joyful curious expression, balloons and bunting in background. Bright cheerful lighting.")},
    {"name": "Baby with Parents", "category": "baby", "theme": "family", "tags": ["baby", "family", "parents"], "aspect_ratio": "9:16",
     "scene_description": _scene("Parents joyfully holding their baby, warm affectionate expressions, softly lit home background with neutral tones. Warm tender natural lighting.")},
    {"name": "Cute Baby Portrait", "category": "baby", "theme": "portrait", "tags": ["baby", "portrait", "cute"], "aspect_ratio": "9:16",
     "scene_description": _scene("A close-up baby portrait with a curious giggling expression, soft pastel outfit, clean neutral studio backdrop. Soft diffused studio lighting.")},

    # ── Travel ─────────────────────────────────────────────────────────────
    {"name": "Snow Mountains", "category": "travel", "theme": "snow", "tags": ["travel", "mountains", "snow"], "aspect_ratio": "16:9",
     "scene_description": _scene("A traveler standing before snow-capped mountain peaks, warm winter jacket, crisp clear sky. Bright natural alpine daylight.")},
    {"name": "Beach Sunset", "category": "travel", "theme": "beach", "tags": ["travel", "beach", "sunset"], "aspect_ratio": "16:9",
     "scene_description": _scene("A traveler walking along a tropical beach at sunset, light summer outfit, palm trees and golden sky in background. Warm golden-hour lighting.")},
    {"name": "European Street", "category": "travel", "theme": "europe", "tags": ["travel", "europe", "street"], "aspect_ratio": "16:9",
     "scene_description": _scene("A traveler strolling down a cobblestone European street, stylish casual outfit, historic buildings and cafes in background. Soft overcast daylight.")},
    {"name": "Desert Safari", "category": "travel", "theme": "desert", "tags": ["travel", "desert", "adventure"], "aspect_ratio": "16:9",
     "scene_description": _scene("A traveler atop a golden sand dune at sunset, flowing scarf and desert attire, vast dunes stretching to the horizon. Warm dramatic desert lighting.")},

    # ── Career ─────────────────────────────────────────────────────────────
    {"name": "Corporate Boardroom", "category": "career", "theme": "corporate", "tags": ["career", "corporate", "professional"], "aspect_ratio": "9:16",
     "scene_description": _scene("A professional in sharp business attire standing confidently in a modern glass boardroom, city skyline visible through windows. Crisp corporate lighting.")},
    {"name": "Doctor at Work", "category": "career", "theme": "medical", "tags": ["career", "doctor", "medical"], "aspect_ratio": "9:16",
     "scene_description": _scene("A doctor in a white coat with a stethoscope, warm reassuring expression, clean modern clinic background. Bright clinical lighting.")},
    {"name": "Entrepreneur Success", "category": "career", "theme": "startup", "tags": ["career", "entrepreneur", "success"], "aspect_ratio": "9:16",
     "scene_description": _scene("An entrepreneur in smart-casual attire standing in a bright modern office, laptop and charts in background, confident pose. Natural office daylight.")},
    {"name": "Government Officer Portrait", "category": "career", "theme": "government", "tags": ["career", "officer", "formal"], "aspect_ratio": "9:16",
     "scene_description": _scene("A government officer in formal uniform attire, dignified composed expression, official office backdrop with flag. Even formal studio lighting.")},

    # ── Traditional ────────────────────────────────────────────────────────
    {"name": "Traditional Saree Portrait", "category": "traditional", "theme": "saree", "tags": ["traditional", "saree", "cultural"], "aspect_ratio": "9:16",
     "scene_description": _scene("A woman in an elegant traditional silk saree with classic jewelry, graceful pose, ornate cultural backdrop. Warm golden traditional lighting.")},
    {"name": "Dhoti Kurta Portrait", "category": "traditional", "theme": "dhoti-kurta", "tags": ["traditional", "dhoti", "cultural"], "aspect_ratio": "9:16",
     "scene_description": _scene("A man in a traditional dhoti-kurta with a turban, dignified confident stance, heritage courtyard backdrop. Warm natural daylight.")},
    {"name": "Regional Folk Attire", "category": "traditional", "theme": "folk", "tags": ["traditional", "folk", "regional"], "aspect_ratio": "9:16",
     "scene_description": _scene("A person in vibrant regional folk attire with traditional jewelry, joyful festive pose, rustic cultural backdrop. Warm festive lighting.")},
    {"name": "Traditional Festival Attire", "category": "traditional", "theme": "festival-attire", "tags": ["traditional", "festival", "cultural"], "aspect_ratio": "9:16",
     "scene_description": _scene("A person dressed in ornate festival attire with traditional accessories, joyful celebratory expression, decorated home backdrop with diyas. Warm glowing lighting.")},

    # ── Spiritual ──────────────────────────────────────────────────────────
    {"name": "Temple Devotion", "category": "spiritual", "theme": "temple", "tags": ["spiritual", "temple", "devotion"], "aspect_ratio": "9:16",
     "scene_description": _scene("A devotee with folded hands in prayer before an ornate temple backdrop, traditional attire, serene devoted expression. Warm temple lamp lighting.")},
    {"name": "Meditation Sunrise", "category": "spiritual", "theme": "meditation", "tags": ["spiritual", "meditation", "sunrise"], "aspect_ratio": "9:16",
     "scene_description": _scene("A person meditating cross-legged at sunrise, simple calm attire, misty hills or riverside backdrop. Soft warm sunrise lighting.")},
    {"name": "Pilgrimage Portrait", "category": "spiritual", "theme": "pilgrimage", "tags": ["spiritual", "pilgrimage", "journey"], "aspect_ratio": "9:16",
     "scene_description": _scene("A pilgrim in simple traditional attire standing before a sacred mountain or river backdrop, peaceful reverent expression. Soft natural daylight.")},
    {"name": "Diya Prayer Moment", "category": "spiritual", "theme": "diya", "tags": ["spiritual", "diya", "prayer"], "aspect_ratio": "9:16",
     "scene_description": _scene("A person lighting a diya lamp with cupped hands, warm devoted expression, softly lit home altar backdrop. Warm intimate diya-glow lighting.")},

    # ── Tribute / Memory Restore ───────────────────────────────────────────
    {"name": "Memory Restore Portrait", "category": "tribute", "theme": "restore", "tags": ["tribute", "memory", "restore"], "aspect_ratio": "9:16",
     "scene_description": _scene("A dignified, lovingly restored portrait of a person, natural authentic skin tones and expression preserved, clean neutral studio backdrop. Soft even lighting.")},
    {"name": "Tribute to Grandparents", "category": "tribute", "theme": "grandparents", "tags": ["tribute", "grandparents", "family"], "aspect_ratio": "9:16",
     "scene_description": _scene("A warm dignified portrait of grandparents together, traditional attire, soft warm home backdrop. Gentle nostalgic lighting.")},
    {"name": "In Loving Memory Frame", "category": "tribute", "theme": "memorial", "tags": ["tribute", "memorial", "remembrance"], "aspect_ratio": "9:16",
     "scene_description": _scene("A respectful memorial-style portrait with a soft floral frame border, calm dignified expression, muted neutral backdrop. Soft solemn lighting.")},
    {"name": "Family Legacy Portrait", "category": "tribute", "theme": "legacy", "tags": ["tribute", "legacy", "family"], "aspect_ratio": "9:16",
     "scene_description": _scene("A multi-generation family portrait rendered with dignity and warmth, traditional attire, heritage home backdrop. Warm golden lighting.")},

    # ── Fantasy ────────────────────────────────────────────────────────────
    {"name": "Royal Fantasy Portrait", "category": "fantasy", "theme": "royal-fantasy", "tags": ["fantasy", "royal", "artistic"], "aspect_ratio": "9:16",
     "scene_description": _scene("A person as royalty in an ornate jeweled crown and regal robes, majestic palace throne room backdrop. Dramatic golden royal lighting.")},
    {"name": "Superhero Transformation", "category": "fantasy", "theme": "superhero", "tags": ["fantasy", "superhero", "action"], "aspect_ratio": "9:16",
     "scene_description": _scene("A person transformed into a heroic superhero costume, confident powerful stance, dramatic city skyline backdrop. Bold cinematic lighting.")},
    {"name": "Mythical Warrior", "category": "fantasy", "theme": "warrior", "tags": ["fantasy", "warrior", "mythical"], "aspect_ratio": "9:16",
     "scene_description": _scene("A person as a mythical warrior in ornate armor, fierce determined expression, epic battlefield or mountain backdrop. Dramatic stormy lighting.")},
    {"name": "Fairy Tale Dream", "category": "fantasy", "theme": "fairy-tale", "tags": ["fantasy", "fairy-tale", "dreamy"], "aspect_ratio": "9:16",
     "scene_description": _scene("A person in an enchanting fairy-tale gown, whimsical dreamy expression, magical glowing forest backdrop. Soft ethereal magical lighting.")},
]

for _t in TEMPLATES:
    _t["negative_prompt"] = NEGATIVE_PROMPT
    _t["base_price_paise"] = BASE_PRICE_PAISE
    _t.setdefault("is_featured", False)
    _t.setdefault("image_url", None)


async def add_templates() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        # Skip any template whose name already exists — safe to re-run.
        existing = await session.execute(text("SELECT name FROM templates"))
        existing_names = {row[0] for row in existing.fetchall()}

        inserted = 0
        for t in TEMPLATES:
            if t["name"] in existing_names:
                continue
            await session.execute(
                text(
                    "INSERT INTO templates "
                    "(name, category, theme, image_url, scene_description, negative_prompt, "
                    " tags, aspect_ratio, asset_keys, base_price_paise, active, language, is_featured) "
                    "VALUES (:name, :category, :theme, :image_url, :scene_description, :negative_prompt, "
                    " :tags, :aspect_ratio, '{}', :base_price_paise, true, 'en', :is_featured)"
                ),
                {**t, "tags": json.dumps(t["tags"])},
            )
            inserted += 1

        await session.commit()
        logger.info("Inserted %d new templates (%d already existed, skipped).",
                    inserted, len(TEMPLATES) - inserted)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(add_templates())
