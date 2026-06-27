"""
Seed initial templates into the database.
Run once after migrations:
    python scripts/seed_templates.py
"""
from __future__ import annotations

import asyncio
import json
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = (
    os.environ["DATABASE_URL"]
    .replace("postgresql://", "postgresql+asyncpg://", 1)
    .replace("postgres://", "postgresql+asyncpg://", 1)
)

TEMPLATES = [
    # ── Wedding ───────────────────────────────────────────────────────────────
    {
        "name": "Floral Wedding Invite",
        "language": "en",
        "theme": "floral",
        "base_price_paise": 2900,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1519225421980-715cb0215aed?w=400&q=80",
            "description": "Elegant floral border wedding invitation with pink and gold tones",
        }),
    },
    {
        "name": "Classic Wedding Invite",
        "language": "en",
        "theme": "classic",
        "base_price_paise": 1900,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1511285560929-80b456fea0bc?w=400&q=80",
            "description": "Timeless white and gold wedding invitation with couple silhouette",
        }),
    },
    {
        "name": "Royal Wedding Invite",
        "language": "en",
        "theme": "royal",
        "base_price_paise": 4900,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1606800052052-a08af7148866?w=400&q=80",
            "description": "Royal Rajasthani-style wedding card with deep red and gold embellishments",
        }),
    },
    {
        "name": "Garden Wedding Invite",
        "language": "en",
        "theme": "garden",
        "base_price_paise": 2500,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1464366400600-7168b8af9bc3?w=400&q=80",
            "description": "Fresh garden wedding invite with green leaves and white flowers",
        }),
    },
    {
        "name": "Mandap Wedding Invite",
        "language": "en",
        "theme": "traditional",
        "base_price_paise": 3500,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1583089892943-e02e5b017b6a?w=400&q=80",
            "description": "Traditional Indian mandap ceremony invitation with marigold and diyas",
        }),
    },

    # ── Birthday ──────────────────────────────────────────────────────────────
    {
        "name": "Birthday Celebration",
        "language": "en",
        "theme": "birthday",
        "base_price_paise": 1500,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1464349095431-e9a21285b5f3?w=400&q=80",
            "description": "Colourful birthday celebration card with balloons and confetti",
        }),
    },
    {
        "name": "Kids Birthday Party",
        "language": "en",
        "theme": "kids-birthday",
        "base_price_paise": 1500,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1530103862676-de8c9debad1d?w=400&q=80",
            "description": "Fun and bright kids birthday party invite with cartoon theme",
        }),
    },

    # ── Baby & Family ─────────────────────────────────────────────────────────
    {
        "name": "Baby Shower",
        "language": "en",
        "theme": "babyshower",
        "base_price_paise": 1500,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1515488042361-ee00e0ddd4e4?w=400&q=80",
            "description": "Soft pastel baby shower invite with cute baby elements",
        }),
    },
    {
        "name": "Naming Ceremony",
        "language": "en",
        "theme": "naming-ceremony",
        "base_price_paise": 1500,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1555252333-9f8e92e65df9?w=400&q=80",
            "description": "Sweet namkaran/naming ceremony invite with baby footprints and flowers",
        }),
    },

    # ── Other Events ──────────────────────────────────────────────────────────
    {
        "name": "Housewarming Invite",
        "language": "en",
        "theme": "housewarming",
        "base_price_paise": 1500,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=400&q=80",
            "description": "Warm griha pravesh / housewarming invitation with home and diyas",
        }),
    },
]


async def seed() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        await session.execute(text("DELETE FROM templates"))
        for t in TEMPLATES:
            await session.execute(
                text(
                    "INSERT INTO templates (name, language, theme, asset_keys, base_price_paise, active) "
                    "VALUES (:name, :language, :theme, cast(:asset_keys as jsonb), :base_price_paise, true)"
                ),
                t,
            )
        await session.commit()
        print(f"Seeded {len(TEMPLATES)} templates.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
