"""
One-off script to reprice all existing templates to a flat 10%-margin price.

All templates share the same underlying generation cost (one gpt-image-2
composite call, ~₹6.25-10.75 depending on 1-4 attached photos). A flat
₹12 (1200 paise) guarantees >=10% margin even in the worst case (4 photos).

Usage:
    DATABASE_URL=postgresql://... python scripts/update_template_prices.py
"""
from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = (
    os.environ.get("DATABASE_URL", "")
    .replace("postgresql://", "postgresql+asyncpg://", 1)
    .replace("postgres://", "postgresql+asyncpg://", 1)
)

NEW_PRICE_PAISE = 1200


async def update_prices() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        result = await session.execute(
            text("UPDATE templates SET base_price_paise = :price"),
            {"price": NEW_PRICE_PAISE},
        )
        await session.commit()
        logger.info("Updated %d templates to base_price_paise=%d (₹%s)",
                    result.rowcount, NEW_PRICE_PAISE, NEW_PRICE_PAISE // 100)


if __name__ == "__main__":
    asyncio.run(update_prices())
