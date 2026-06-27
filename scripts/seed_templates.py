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

# Preview images from Unsplash (free, no API key needed for direct URLs)
TEMPLATES = [
    {
        "name": "Floral Wedding Invite",
        "language": "hi",
        "theme": "floral",
        "base_price_paise": 2900,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1519225421980-715cb0215aed?w=400&q=80"
        }),
    },
    {
        "name": "Classic Wedding Invite",
        "language": "hi",
        "theme": "classic",
        "base_price_paise": 1900,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1511285560929-80b456fea0bc?w=400&q=80"
        }),
    },
    {
        "name": "Royal Wedding Invite",
        "language": "hi",
        "theme": "royal",
        "base_price_paise": 4900,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1606800052052-a08af7148866?w=400&q=80"
        }),
    },
    {
        "name": "Floral Wedding Invite",
        "language": "en",
        "theme": "floral",
        "base_price_paise": 2900,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1519225421980-715cb0215aed?w=400&q=80"
        }),
    },
    {
        "name": "Classic Wedding Invite",
        "language": "en",
        "theme": "classic",
        "base_price_paise": 1900,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1511285560929-80b456fea0bc?w=400&q=80"
        }),
    },
    {
        "name": "పూల పెళ్లి ఆహ్వానం",
        "language": "te",
        "theme": "floral",
        "base_price_paise": 2900,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1519225421980-715cb0215aed?w=400&q=80"
        }),
    },
    {
        "name": "Birthday Celebration",
        "language": "hi",
        "theme": "birthday",
        "base_price_paise": 1500,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1464349095431-e9a21285b5f3?w=400&q=80"
        }),
    },
    {
        "name": "Baby Shower",
        "language": "hi",
        "theme": "babyshower",
        "base_price_paise": 1500,
        "asset_keys": json.dumps({
            "preview_url": "https://images.unsplash.com/photo-1515488042361-ee00e0ddd4e4?w=400&q=80"
        }),
    },
]


async def seed() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        # Clear existing templates and re-seed with images
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
        print(f"Seeded {len(TEMPLATES)} templates with preview images.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
