"""
Tests for AgentService — commission crediting, idempotency, earnings queries.

Why commissions ride on the same ledger
----------------------------------------
The credit_ledger table is append-only: every credit mutation (purchase, spend,
refund, commission) is a new row with a signed delta_paise.  A user's balance is
always SUM(delta_paise); the reason column filters to a specific category.

For agents this means:
  • An agent's wallet balance includes unspent commissions.  They can use those
    credits to place orders for themselves, exactly like any other user.
  • The earnings endpoint filters reason=commission so it shows only commission
    income, not the agent's own purchases.
  • Idempotency is enforced by the unique constraint on idempotency_key —
    "commission:{order_id}" — so retried workers never double-pay.
  • The commission entry and order.status=done land in the same DB transaction
    (see workers/jobs.py), so there is no window where the order is done but the
    commission is unpaid and unrecoverable.

Uses live PostgreSQL (skipped when not reachable).
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.ledger import CreditLedger, LedgerReason
from app.models.order import Order, OrderStatus
from app.services.agent import AgentService, NotAnAgentError
from app.services.credits import CreditsService, LedgerReason as CR

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://weddingapp:weddingapp@localhost:5432/weddingapp",
)


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def engine():
    eng = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
    try:
        async with eng.connect() as c:
            await c.execute(text("SELECT 1"))
    except Exception:
        await eng.dispose()
        pytest.skip("PostgreSQL not reachable")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture()
async def db(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        yield sess
        await sess.rollback()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _make_consumer(db: AsyncSession) -> int:
    """Insert a consumer user, return user_id."""
    result = await db.execute(
        text(
            "INSERT INTO users (phone, name, preferred_language, role) "
            "VALUES (:ph, 'Consumer', 'hi', 'consumer') RETURNING id"
        ),
        {"ph": f"+91c{uuid.uuid4().hex[:12]}"},
    )
    uid = result.scalar_one()
    await db.flush()
    return uid


async def _make_agent(db: AsyncSession, commission_rate_bps: int = 150) -> int:
    """Insert an agent user + agent profile, return user_id."""
    result = await db.execute(
        text(
            "INSERT INTO users (phone, name, preferred_language, role) "
            "VALUES (:ph, 'Agent', 'hi', 'agent') RETURNING id"
        ),
        {"ph": f"+91a{uuid.uuid4().hex[:12]}"},
    )
    agent_user_id = result.scalar_one()
    await db.execute(
        text(
            "INSERT INTO agents (user_id, commission_rate_bps, status) "
            "VALUES (:uid, :bps, 'active')"
        ),
        {"uid": agent_user_id, "bps": commission_rate_bps},
    )
    await db.flush()
    return agent_user_id


async def _make_template(db: AsyncSession, price_paise: int = 5_000) -> int:
    result = await db.execute(
        text(
            "INSERT INTO templates (name, language, theme, asset_keys, base_price_paise, active) "
            "VALUES ('Test', 'hi', 'floral', '{}', :price, true) RETURNING id"
        ),
        {"price": price_paise},
    )
    tid = result.scalar_one()
    await db.flush()
    return tid


async def _make_order(
    db: AsyncSession,
    user_id: int,
    template_id: int,
    *,
    price_paise: int = 5_000,
    agent_id: int | None = None,
    status: OrderStatus = OrderStatus.done,
) -> Order:
    import json

    result = await db.execute(
        text(
            "INSERT INTO orders "
            "(user_id, agent_id, template_id, input_payload, price_paise, idempotency_key, status) "
            "VALUES (:uid, :aid, :tid, cast(:payload as jsonb), :price, :ikey, "
            "cast(:status as order_status)) RETURNING id"
        ),
        {
            "uid": user_id,
            "aid": agent_id,
            "tid": template_id,
            "payload": json.dumps({"theme": "floral"}),
            "price": price_paise,
            "ikey": uuid.uuid4().hex,
            "status": status.value,
        },
    )
    order_id = result.scalar_one()
    await db.flush()

    order = await db.get(Order, order_id)
    assert order is not None
    return order


async def _get_commission_entries(db: AsyncSession, user_id: int) -> list[CreditLedger]:
    result = await db.execute(
        select(CreditLedger)
        .where(
            CreditLedger.user_id == user_id,
            CreditLedger.reason == LedgerReason.commission,
        )
        .order_by(CreditLedger.created_at)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Tests: commission payment
# ---------------------------------------------------------------------------


async def test_commission_paid_for_done_order(db: AsyncSession):
    """A done agent-order triggers a correct commission credit."""
    consumer_id = await _make_consumer(db)
    agent_id = await _make_agent(db, commission_rate_bps=200)  # 2 %
    template_id = await _make_template(db, price_paise=10_000)

    order = await _make_order(
        db, consumer_id, template_id, price_paise=10_000, agent_id=agent_id
    )

    svc = AgentService(db)
    entry = await svc.pay_commission(order)
    await db.commit()

    assert entry is not None
    # 10_000 * 200 // 10_000 = 200 paise
    assert entry.delta_paise == 200
    assert entry.reason == LedgerReason.commission
    assert entry.ref_type == "order"
    assert entry.ref_id == order.id
    assert entry.idempotency_key == f"commission:{order.id}"

    credits = CreditsService(db)
    balance = await credits.get_balance(agent_id)
    assert balance == 200


async def test_commission_paid_exactly_once_on_double_call(db: AsyncSession):
    """
    Calling pay_commission twice with the same order returns the pre-existing
    entry on the second call — no double-pay, same ledger row count.
    """
    consumer_id = await _make_consumer(db)
    agent_id = await _make_agent(db, commission_rate_bps=150)
    template_id = await _make_template(db, price_paise=5_000)

    order = await _make_order(
        db, consumer_id, template_id, price_paise=5_000, agent_id=agent_id
    )

    svc = AgentService(db)

    entry1 = await svc.pay_commission(order)
    await db.commit()

    entry2 = await svc.pay_commission(order)
    await db.commit()

    # Both calls return an entry with the same id
    assert entry1 is not None
    assert entry2 is not None
    assert entry1.id == entry2.id

    # Exactly one row in the ledger
    rows = await _get_commission_entries(db, agent_id)
    assert len(rows) == 1
    assert rows[0].delta_paise == 75  # 5_000 * 150 // 10_000 = 75


async def test_commission_math_150bps(db: AsyncSession):
    """150 bps on 5 000 paise = 75 paise (integer, no float)."""
    consumer_id = await _make_consumer(db)
    agent_id = await _make_agent(db, commission_rate_bps=150)
    template_id = await _make_template(db, price_paise=5_000)
    order = await _make_order(
        db, consumer_id, template_id, price_paise=5_000, agent_id=agent_id
    )

    entry = await AgentService(db).pay_commission(order)
    assert entry is not None
    assert entry.delta_paise == 75


async def test_commission_math_rounds_down(db: AsyncSession):
    """Integer division floors — 3 paise * 150 bps = 0 paise → skipped."""
    consumer_id = await _make_consumer(db)
    agent_id = await _make_agent(db, commission_rate_bps=150)
    template_id = await _make_template(db, price_paise=3)
    order = await _make_order(
        db, consumer_id, template_id, price_paise=3, agent_id=agent_id
    )

    entry = await AgentService(db).pay_commission(order)
    # 3 * 150 // 10_000 = 0 → service returns None, no ledger row
    assert entry is None

    rows = await _get_commission_entries(db, agent_id)
    assert len(rows) == 0


async def test_no_commission_on_rejected_order(db: AsyncSession):
    """Rejected orders never earn commission."""
    consumer_id = await _make_consumer(db)
    agent_id = await _make_agent(db, commission_rate_bps=200)
    template_id = await _make_template(db, price_paise=5_000)

    order = await _make_order(
        db,
        consumer_id,
        template_id,
        price_paise=5_000,
        agent_id=agent_id,
        status=OrderStatus.rejected,
    )

    entry = await AgentService(db).pay_commission(order)
    assert entry is None

    rows = await _get_commission_entries(db, agent_id)
    assert len(rows) == 0


async def test_no_commission_on_refunded_order(db: AsyncSession):
    """Refunded orders never earn commission."""
    consumer_id = await _make_consumer(db)
    agent_id = await _make_agent(db, commission_rate_bps=200)
    template_id = await _make_template(db, price_paise=5_000)

    order = await _make_order(
        db,
        consumer_id,
        template_id,
        price_paise=5_000,
        agent_id=agent_id,
        status=OrderStatus.refunded,
    )

    entry = await AgentService(db).pay_commission(order)
    assert entry is None


async def test_no_commission_without_agent_id(db: AsyncSession):
    """Consumer orders placed directly (agent_id=None) earn nothing."""
    consumer_id = await _make_consumer(db)
    template_id = await _make_template(db, price_paise=5_000)

    order = await _make_order(
        db, consumer_id, template_id, price_paise=5_000, agent_id=None
    )

    entry = await AgentService(db).pay_commission(order)
    assert entry is None


async def test_no_commission_when_rate_is_zero(db: AsyncSession):
    """An agent with commission_rate_bps=0 earns nothing."""
    consumer_id = await _make_consumer(db)
    agent_id = await _make_agent(db, commission_rate_bps=0)
    template_id = await _make_template(db, price_paise=5_000)

    order = await _make_order(
        db, consumer_id, template_id, price_paise=5_000, agent_id=agent_id
    )

    entry = await AgentService(db).pay_commission(order)
    assert entry is None


async def test_commission_appears_in_wallet_balance(db: AsyncSession):
    """Agent's wallet balance includes commission — they can spend it."""
    consumer_id = await _make_consumer(db)
    agent_id = await _make_agent(db, commission_rate_bps=500)  # 5 %
    template_id = await _make_template(db, price_paise=8_000)

    order = await _make_order(
        db, consumer_id, template_id, price_paise=8_000, agent_id=agent_id
    )

    await AgentService(db).pay_commission(order)
    await db.commit()

    # 8_000 * 500 // 10_000 = 400 paise
    credits = CreditsService(db)
    balance = await credits.get_balance(agent_id)
    assert balance == 400


# ---------------------------------------------------------------------------
# Tests: get_earnings
# ---------------------------------------------------------------------------


async def test_get_earnings_empty(db: AsyncSession):
    """Agent with no completed orders has zero earnings."""
    agent_id = await _make_agent(db, commission_rate_bps=150)

    result = await AgentService(db).get_earnings(agent_id)

    assert result.total_commission_paise == 0
    assert result.entries == []


async def test_get_earnings_multiple_orders(db: AsyncSession):
    """Total and entries reflect all commission rows for this agent."""
    consumer_id = await _make_consumer(db)
    agent_id = await _make_agent(db, commission_rate_bps=200)  # 2 %
    template_id = await _make_template(db, price_paise=5_000)

    svc = AgentService(db)

    # Create and pay commission on three orders
    for _ in range(3):
        order = await _make_order(
            db, consumer_id, template_id, price_paise=5_000, agent_id=agent_id
        )
        await svc.pay_commission(order)
        await db.commit()

    result = await svc.get_earnings(agent_id)

    # 5_000 * 200 // 10_000 = 100 paise per order × 3 = 300
    assert result.total_commission_paise == 300
    assert len(result.entries) == 3
    assert all(e.delta_paise == 100 for e in result.entries)
    assert all(e.ref_type == "order" for e in result.entries)


async def test_get_earnings_only_counts_commissions_not_purchases(db: AsyncSession):
    """get_earnings filters to reason=commission; agent's own purchases are excluded."""
    agent_id = await _make_agent(db, commission_rate_bps=200)

    # Give the agent some credits via a purchase (different reason)
    credits = CreditsService(db)
    await credits.credit(
        user_id=agent_id,
        delta_paise=50_000,
        reason=LedgerReason.purchase,
        idempotency_key=f"test:purchase:{uuid.uuid4().hex}",
    )
    await db.commit()

    # Zero commissions yet
    result = await AgentService(db).get_earnings(agent_id)
    assert result.total_commission_paise == 0
    assert result.entries == []


async def test_get_earnings_raises_for_non_agent(db: AsyncSession):
    """get_earnings raises NotAnAgentError for a user without an agent profile."""
    consumer_id = await _make_consumer(db)

    with pytest.raises(NotAnAgentError):
        await AgentService(db).get_earnings(consumer_id)


async def test_commission_isolation_between_agents(db: AsyncSession):
    """Commissions for different agents are independent — no cross-contamination."""
    consumer_id = await _make_consumer(db)
    agent1_id = await _make_agent(db, commission_rate_bps=100)
    agent2_id = await _make_agent(db, commission_rate_bps=300)
    template_id = await _make_template(db, price_paise=10_000)

    svc = AgentService(db)

    order1 = await _make_order(
        db, consumer_id, template_id, price_paise=10_000, agent_id=agent1_id
    )
    await svc.pay_commission(order1)

    order2 = await _make_order(
        db, consumer_id, template_id, price_paise=10_000, agent_id=agent2_id
    )
    await svc.pay_commission(order2)
    await db.commit()

    result1 = await svc.get_earnings(agent1_id)
    result2 = await svc.get_earnings(agent2_id)

    # 10_000 * 100 // 10_000 = 100 paise
    assert result1.total_commission_paise == 100
    # 10_000 * 300 // 10_000 = 300 paise
    assert result2.total_commission_paise == 300
