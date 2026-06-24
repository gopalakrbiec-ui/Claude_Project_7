"""
Integration tests for the PostgreSQL schema.

Requires a real PostgreSQL instance. The DATABASE_URL env var must point at it.
In CI / docker-compose this is the postgres service; locally it's the host PG cluster.

Tests are skipped automatically if the DB is unreachable so they never block
the unit-test suite that runs without Docker.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://weddingapp:weddingapp@localhost:5432/weddingapp",
)

# Module-level reachability flag set by the first fixture call.
_db_reachable: bool | None = None


@pytest_asyncio.fixture()
async def db_session():
    global _db_reachable
    engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        _db_reachable = True
    except Exception:
        await engine.dispose()
        pytest.skip("PostgreSQL not reachable — skipping schema integration tests")

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_TABLES = {
    "users",
    "agents",
    "credit_ledger",
    "payments",
    "orders",
    "generation_jobs",
    "templates",
}


async def _table_columns(session: AsyncSession, table: str) -> set[str]:
    result = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    )
    return {row[0] for row in result}


async def _indexes(session: AsyncSession, table: str) -> set[str]:
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = :t"
        ),
        {"t": table},
    )
    return {row[0] for row in result}


async def _unique_constraints(session: AsyncSession, table: str) -> set[str]:
    result = await session.execute(
        text(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_schema = 'public' AND table_name = :t "
            "AND constraint_type = 'UNIQUE'"
        ),
        {"t": table},
    )
    return {row[0] for row in result}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_tables_exist(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
    )
    tables = {row[0] for row in result}
    assert EXPECTED_TABLES.issubset(tables), f"Missing tables: {EXPECTED_TABLES - tables}"


@pytest.mark.asyncio
async def test_users_columns(db_session: AsyncSession) -> None:
    cols = await _table_columns(db_session, "users")
    assert {"id", "phone", "name", "preferred_language", "role", "created_at"}.issubset(cols)


@pytest.mark.asyncio
async def test_users_phone_unique_index(db_session: AsyncSession) -> None:
    indexes = await _indexes(db_session, "users")
    assert any("phone" in idx for idx in indexes), f"No phone index found in: {indexes}"


@pytest.mark.asyncio
async def test_agents_columns(db_session: AsyncSession) -> None:
    cols = await _table_columns(db_session, "agents")
    assert {"user_id", "commission_rate_bps", "payout_upi_vpa", "status"}.issubset(cols)


@pytest.mark.asyncio
async def test_credit_ledger_columns(db_session: AsyncSession) -> None:
    cols = await _table_columns(db_session, "credit_ledger")
    assert {
        "id", "user_id", "delta_paise", "reason",
        "ref_type", "ref_id", "idempotency_key", "created_at",
    }.issubset(cols)


@pytest.mark.asyncio
async def test_credit_ledger_idempotency_unique(db_session: AsyncSession) -> None:
    constraints = await _unique_constraints(db_session, "credit_ledger")
    assert "uq_credit_ledger_idempotency_key" in constraints


@pytest.mark.asyncio
async def test_credit_ledger_user_id_index(db_session: AsyncSession) -> None:
    indexes = await _indexes(db_session, "credit_ledger")
    assert any("user_id" in idx for idx in indexes), f"No user_id index: {indexes}"


@pytest.mark.asyncio
async def test_payments_idempotency_unique(db_session: AsyncSession) -> None:
    constraints = await _unique_constraints(db_session, "payments")
    assert "uq_payments_idempotency_key" in constraints


@pytest.mark.asyncio
async def test_payments_columns(db_session: AsyncSession) -> None:
    cols = await _table_columns(db_session, "payments")
    assert {
        "id", "user_id", "gateway", "gateway_order_id",
        "gateway_payment_id", "amount_paise", "status",
        "idempotency_key", "raw_webhook", "created_at",
    }.issubset(cols)


@pytest.mark.asyncio
async def test_orders_columns(db_session: AsyncSession) -> None:
    cols = await _table_columns(db_session, "orders")
    assert {
        "id", "user_id", "agent_id", "template_id",
        "input_payload", "price_paise", "status", "created_at",
    }.issubset(cols)


@pytest.mark.asyncio
async def test_generation_jobs_columns(db_session: AsyncSession) -> None:
    cols = await _table_columns(db_session, "generation_jobs")
    assert {
        "id", "order_id", "status", "provider",
        "cost_paise", "error", "output_keys", "moderation_result", "created_at",
    }.issubset(cols)


@pytest.mark.asyncio
async def test_templates_columns(db_session: AsyncSession) -> None:
    cols = await _table_columns(db_session, "templates")
    assert {
        "id", "name", "language", "theme",
        "asset_keys", "base_price_paise", "active",
    }.issubset(cols)


@pytest.mark.asyncio
async def test_balance_query_runs(db_session: AsyncSession) -> None:
    """
    The canonical balance query must parse and plan correctly.
    Result is NULL (no rows) on an empty DB — that's fine; we're testing the query shape.
    """
    result = await db_session.execute(
        text("SELECT COALESCE(SUM(delta_paise), 0) FROM credit_ledger WHERE user_id = :uid"),
        {"uid": 0},
    )
    balance = result.scalar()
    assert balance == 0  # no entries for user 0


@pytest.mark.asyncio
async def test_insert_and_ledger_balance(db_session: AsyncSession) -> None:
    """Round-trip: insert a user + ledger entries, assert balance = SUM."""
    # Insert a test user
    await db_session.execute(
        text(
            "INSERT INTO users (phone, name, preferred_language, role) "
            "VALUES (:ph, 'Test User', 'hi', 'consumer') "
            "ON CONFLICT (phone) DO NOTHING"
        ),
        {"ph": "+91-test-9999999999"},
    )
    result = await db_session.execute(
        text("SELECT id FROM users WHERE phone = :ph"),
        {"ph": "+91-test-9999999999"},
    )
    user_id = result.scalar_one()

    # Credit 5000 paise
    await db_session.execute(
        text(
            "INSERT INTO credit_ledger (user_id, delta_paise, reason, idempotency_key) "
            "VALUES (:uid, 5000, 'purchase', 'test-idem-1')"
        ),
        {"uid": user_id},
    )
    # Debit 1500 paise
    await db_session.execute(
        text(
            "INSERT INTO credit_ledger (user_id, delta_paise, reason, idempotency_key) "
            "VALUES (:uid, -1500, 'spend', 'test-idem-2')"
        ),
        {"uid": user_id},
    )

    result = await db_session.execute(
        text("SELECT SUM(delta_paise) FROM credit_ledger WHERE user_id = :uid"),
        {"uid": user_id},
    )
    balance = result.scalar()
    assert balance == 3500, f"Expected 3500 paise, got {balance}"
    await db_session.rollback()  # don't pollute the DB between test runs
