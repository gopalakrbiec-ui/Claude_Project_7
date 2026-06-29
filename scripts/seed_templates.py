"""
Seed initial templates into the database.
Run once after migrations:
    python scripts/seed_templates.py
"""
from __future__ import annotations

import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = (
    os.environ["DATABASE_URL"]
    .replace("postgresql://", "postgresql+asyncpg://", 1)
    .replace("postgres://", "postgresql+asyncpg://", 1)
)

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
# image_url      — the reference/style image shown in the Flutter grid
# scene_description — fed to Claude to craft the generation prompt
# category       — groups templates in the UI (wedding, birthday, fashion, etc.)
# theme          — sub-style within the category
# ---------------------------------------------------------------------------

TEMPLATES = [

    # ── Wedding ───────────────────────────────────────────────────────────────
    {
        "name": "Floral Mandap",
        "category": "wedding",
        "theme": "floral",
        "image_url": "https://images.unsplash.com/photo-1583089892943-e02e5b017b6a?w=600&q=85",
        "scene_description": (
            "Traditional Indian wedding mandap decorated with fresh marigold garlands, rose petals, "
            "and flickering oil diyas. Warm golden hour light. Ultra-realistic DSLR photography, "
            "8K resolution, shallow depth of field."
        ),
        "base_price_paise": 2900,
    },
    {
        "name": "Royal Sherwani",
        "category": "wedding",
        "theme": "royal",
        "image_url": "https://images.unsplash.com/photo-1594736797933-d0501ba2fe65?w=600&q=85",
        "scene_description": (
            "Groom in a rich red and gold embroidered sherwani standing in front of a Rajasthani palace "
            "archway. Regal, cinematic lighting. 8K DSLR portrait, bokeh background."
        ),
        "base_price_paise": 4900,
    },
    {
        "name": "Bridal Lehenga",
        "category": "wedding",
        "theme": "bridal",
        "image_url": "https://images.unsplash.com/photo-1511285560929-80b456fea0bc?w=600&q=85",
        "scene_description": (
            "Bride in an ornate red and gold bridal lehenga with heavy jewelry, standing in a lush "
            "flower-filled garden at golden hour. Professional wedding photography, 8K, cinematic."
        ),
        "base_price_paise": 4900,
    },
    {
        "name": "Garden Wedding",
        "category": "wedding",
        "theme": "garden",
        "image_url": "https://images.unsplash.com/photo-1464366400600-7168b8af9bc3?w=600&q=85",
        "scene_description": (
            "Couple standing under a floral arch surrounded by white roses and greenery. "
            "Soft natural light, elegant outdoor setting. 8K DSLR wedding portrait."
        ),
        "base_price_paise": 3500,
    },
    {
        "name": "Royal Couple Portrait",
        "category": "wedding",
        "theme": "royal-couple",
        "image_url": "https://images.unsplash.com/photo-1519225421980-715cb0215aed?w=600&q=85",
        "scene_description": (
            "Couple dressed in matching royal outfits — bride in lehenga, groom in sherwani — "
            "seated on an ornate gold throne. Palace backdrop, dramatic lighting. 8K cinematic."
        ),
        "base_price_paise": 5900,
    },

    # ── Birthday ──────────────────────────────────────────────────────────────
    {
        "name": "Grand Birthday",
        "category": "birthday",
        "theme": "grand",
        "image_url": "https://images.unsplash.com/photo-1464349095431-e9a21285b5f3?w=600&q=85",
        "scene_description": (
            "Person celebrating their birthday surrounded by golden balloons, confetti, and a "
            "tiered cake. Joyful party atmosphere. 8K DSLR portrait, warm celebratory lighting."
        ),
        "base_price_paise": 1500,
    },
    {
        "name": "Kids Birthday Party",
        "category": "birthday",
        "theme": "kids",
        "image_url": "https://images.unsplash.com/photo-1530103862676-de8c9debad1d?w=600&q=85",
        "scene_description": (
            "Child at a colorful birthday party with balloons, confetti, and a cartoon-themed cake. "
            "Bright cheerful setting. 8K professional photography."
        ),
        "base_price_paise": 1500,
    },
    {
        "name": "Milestone Birthday (50/60/75)",
        "category": "birthday",
        "theme": "milestone",
        "image_url": "https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=600&q=85",
        "scene_description": (
            "Elegant milestone birthday portrait with golden '50' or milestone number decor, "
            "warm candlelight, and flowers. Dignified, celebratory. 8K DSLR."
        ),
        "base_price_paise": 2500,
    },

    # ── Fashion & Portrait ────────────────────────────────────────────────────
    {
        "name": "Business Suit Portrait",
        "category": "fashion",
        "theme": "business",
        "image_url": "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=600&q=85",
        "scene_description": (
            "Professional portrait of a person in a sharp tailored business suit, standing in a "
            "modern glass office building. Confident posture, natural window light. 8K DSLR."
        ),
        "base_price_paise": 1900,
    },
    {
        "name": "Traditional Kurta Look",
        "category": "fashion",
        "theme": "traditional-men",
        "image_url": "https://images.unsplash.com/photo-1566492031773-4f4e44671857?w=600&q=85",
        "scene_description": (
            "Man in an elegant embroidered kurta-pajama standing against a haveli wall with "
            "floral decorations. Warm evening light. Ultra-realistic 8K DSLR portrait."
        ),
        "base_price_paise": 1900,
    },
    {
        "name": "Saree Elegance",
        "category": "fashion",
        "theme": "traditional-women",
        "image_url": "https://images.unsplash.com/photo-1583391733956-3750e0ff4e8b?w=600&q=85",
        "scene_description": (
            "Woman in a silk Kanjivaram saree with gold jewelry, standing in a sunlit temple "
            "courtyard with floral rangoli. Graceful, editorial 8K DSLR photography."
        ),
        "base_price_paise": 1900,
    },
    {
        "name": "Bollywood Glamour",
        "category": "fashion",
        "theme": "glamour",
        "image_url": "https://images.unsplash.com/photo-1596557406925-8a621e26f8e8?w=600&q=85",
        "scene_description": (
            "Glamorous Bollywood-style portrait with dramatic studio lighting, sparkly outfit, "
            "and magazine-quality makeup. Cinematic 8K high-fashion photography."
        ),
        "base_price_paise": 2900,
    },

    # ── Baby & Family ─────────────────────────────────────────────────────────
    {
        "name": "Baby Shower",
        "category": "family",
        "theme": "babyshower",
        "image_url": "https://images.unsplash.com/photo-1515488042361-ee00e0ddd4e4?w=600&q=85",
        "scene_description": (
            "Soft pastel baby shower setting with balloons, flowers, and a teddy bear. "
            "Joyful expecting-mother portrait. 8K natural light photography."
        ),
        "base_price_paise": 1500,
    },
    {
        "name": "Naming Ceremony",
        "category": "family",
        "theme": "naming-ceremony",
        "image_url": "https://images.unsplash.com/photo-1555252333-9f8e92e65df9?w=600&q=85",
        "scene_description": (
            "Namkaran ceremony setting with baby in traditional dress held by family, "
            "surrounded by marigold flowers and diyas. Warm, intimate 8K portrait."
        ),
        "base_price_paise": 1500,
    },
    {
        "name": "Family Portrait",
        "category": "family",
        "theme": "family",
        "image_url": "https://images.unsplash.com/photo-1609220136736-443140cffec6?w=600&q=85",
        "scene_description": (
            "Happy Indian family in matching ethnic outfits seated in a beautifully decorated "
            "living room. Warm golden light, joyful expressions. Professional 8K DSLR."
        ),
        "base_price_paise": 2500,
    },

    # ── Festival ──────────────────────────────────────────────────────────────
    {
        "name": "Diwali Celebration",
        "category": "festival",
        "theme": "diwali",
        "image_url": "https://images.unsplash.com/photo-1605600659873-d808a13e4d9a?w=600&q=85",
        "scene_description": (
            "Person in festive Indian ethnic wear surrounded by glowing diyas, sparklers, and "
            "rangoli at Diwali. Warm festive atmosphere. 8K DSLR photography."
        ),
        "base_price_paise": 1500,
    },
    {
        "name": "Housewarming (Griha Pravesh)",
        "category": "festival",
        "theme": "housewarming",
        "image_url": "https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=600&q=85",
        "scene_description": (
            "Family at a Griha Pravesh ceremony at the entrance of a new home decorated with "
            "marigold torans, diyas, and a rangoli. Joyful, auspicious setting. 8K portrait."
        ),
        "base_price_paise": 1500,
    },
]


async def seed() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        await session.execute(text("TRUNCATE templates RESTART IDENTITY CASCADE"))
        for t in TEMPLATES:
            await session.execute(
                text(
                    "INSERT INTO templates "
                    "(name, category, theme, image_url, scene_description, asset_keys, base_price_paise, active, language) "
                    "VALUES (:name, :category, :theme, :image_url, :scene_description, '{}', :base_price_paise, true, 'en')"
                ),
                t,
            )
        await session.commit()
        print(f"Seeded {len(TEMPLATES)} templates.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
