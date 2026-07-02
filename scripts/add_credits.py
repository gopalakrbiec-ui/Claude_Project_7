"""
One-off script to add credits to a user by email or phone.

Usage:
    DATABASE_URL=postgresql://... python scripts/add_credits.py \
        --email gopal.rms4@gmail.com --amount 100000
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = (
    os.environ.get("DATABASE_URL", "")
    .replace("postgresql://", "postgresql+asyncpg://", 1)
    .replace("postgres://", "postgresql+asyncpg://", 1)
)


async def add_credits(email: str | None, phone: str | None, amount_paise: int) -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        # Find user
        if email:
            row = await session.execute(text("SELECT id FROM users WHERE email = :e"), {"e": email})
        else:
            row = await session.execute(text("SELECT id FROM users WHERE phone = :p"), {"p": phone})
        user = row.fetchone()
        if not user:
            logger.error("User not found: email=%s phone=%s", email, phone)
            return

        user_id = user[0]

        # Insert ledger entry directly
        idem_key = f"manual:{user_id}:{uuid.uuid4()}"
        await session.execute(text("""
            INSERT INTO credit_ledger (user_id, delta_paise, reason, ref_type, ref_id, idempotency_key, created_at)
            VALUES (:uid, :delta, 'purchase', 'dev', 0, :idem, NOW())
            ON CONFLICT (idempotency_key) DO NOTHING
        """), {"uid": user_id, "delta": amount_paise, "idem": idem_key})
        await session.commit()

        # Show new balance
        bal = await session.execute(text("SELECT COALESCE(SUM(delta_paise), 0) FROM credit_ledger WHERE user_id = :uid"), {"uid": user_id})
        balance = bal.scalar()
        logger.info("Done. user_id=%s balance=₹%s", user_id, balance // 100)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", help="User email")
    parser.add_argument("--phone", help="User phone")
    parser.add_argument("--amount", type=int, default=100000, help="Amount in paise (default 100000 = ₹1000)")
    args = parser.parse_args()

    if not args.email and not args.phone:
        parser.error("Provide --email or --phone")

    asyncio.run(add_credits(args.email, args.phone, args.amount))
