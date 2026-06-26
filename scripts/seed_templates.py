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

DATABASE_URL = os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://", 1).replace("postgres://", "postgresql+asyncpg://", 1)

TEMPLATES = [
    {
        "name": "Floral Wedding Invite",
        "language": "hi",
        "theme": "floral",
        "base_price_paise": 2900,
        "asset_keys": "{}",
    },
    {
        "name": "Classic Wedding Invite",
        "language": "hi",
        "theme": "classic",
        "base_price_paise": 1900,
        "asset_keys": "{}",
    },
    {
        "name": "Royal Wedding Invite",
        "language": "hi",
        "theme": "royal",
        "base_price_paise": 4900,
        "asset_keys": "{}",
    },
    {
        "name": "Floral Wedding Invite",
        "language": "en",
        "theme": "floral",
        "base_price_paise": 2900,
        "asset_keys": "{}",
    },
    {
        "name": "Classic Wedding Invite",
        "language": "en",
        "theme": "classic",
        "base_price_paise": 1900,
        "asset_keys": "{}",
    },
    {
        "name": "పూల పెళ్లి ఆహ్వానం",
        "language": "te",
        "theme": "floral",
        "base_price_paise": 2900,
        "asset_keys": "{}",
    },
    {
        "name": "Birthday Celebration",
        "language": "hi",
        "theme": "birthday",
        "base_price_paise": 1500,
        "asset_keys": "{}",
    },
    {
        "name": "Baby Shower",
        "language": "hi",
        "theme": "babyshower",
        "base_price_paise": 1500,
        "asset_keys": "{}",
    },
]


async def seed() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        for t in TEMPLATES:
            await session.execute(
                text(
                    "INSERT INTO templates (name, language, theme, asset_keys, base_price_paise, active) "
                    "VALUES (:name, :language, :theme, cast(:asset_keys as jsonb), :base_price_paise, true) "
                    "ON CONFLICT DO NOTHING"
                ),
                t,
            )
        await session.commit()
        print(f"Seeded {len(TEMPLATES)} templates.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
