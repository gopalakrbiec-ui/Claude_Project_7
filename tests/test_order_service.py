"""
Tests for OrderService — order creation, credit debit, idempotency, enqueue.

Uses live PostgreSQL (skipped when not reachable).
Uses LoggingJobQueue (in-process stub) to capture enqueued jobs without Redis.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.queue import LoggingJobQueue
from app.models.ledger import CreditLedger
from app.models.order import Order, OrderStatus
from app.models.template import Template
from app.services.credits import CreditsService, InsufficientBalanceError, LedgerRef, LedgerReason
from app.services.order import OrderService, TemplateNotFoundError

# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://weddingapp:weddingapp@localhost:5432/weddingapp",
)


@pytest_asyncio.fixture()
async def engine():
    eng = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True, pool_size=10)
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
# Seed fixtures
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession) -> int:
    result = await db.execute(
        text(
            "INSERT INTO users (phone, name, preferred_language, role) "
            "VALUES (:ph, 'Test', 'hi', 'consumer') RETURNING id"
        ),
        {"ph": f"+91ord{uuid.uuid4().hex[:10]}"},
    )
    uid = result.scalar_one()
    await db.flush()
    return uid


async def _make_template(db: AsyncSession, price_paise: int = 5_000) -> int:
    result = await db.execute(
        text(
            "INSERT INTO templates (name, language, theme, asset_keys, base_price_paise, active) "
            "VALUES ('Wedding Poster', 'hi', 'floral', '{}', :price, true) RETURNING id"
        ),
        {"price": price_paise},
    )
    tid = result.scalar_one()
    await db.flush()
    return tid


async def _credit_user(db: AsyncSession, user_id: int, amount: int) -> None:
    svc = CreditsService(db)
    await svc.credit(
        user_id=user_id,
        delta_paise=amount,
        reason=LedgerReason.purchase,
        idempotency_key=f"seed:{uuid.uuid4().hex}",
    )
    await db.commit()


async def _get_balance(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(
        select(func.coalesce(func.sum(CreditLedger.delta_paise), 0)).where(
            CreditLedger.user_id == user_id
        )
    )
    return int(result.scalar_one())


def _queue() -> LoggingJobQueue:
    return LoggingJobQueue()


def _ikey() -> str:
    return uuid.uuid4().hex


def _svc(db: AsyncSession, queue: LoggingJobQueue | None = None) -> tuple[OrderService, LoggingJobQueue]:
    q = queue or _queue()
    return OrderService(session=db, queue=q), q


# ---------------------------------------------------------------------------
# Successful order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_order_creates_row(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    tid = await _make_template(db, price_paise=3_000)
    await _credit_user(db, uid, 10_000)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        svc, q = _svc(sess)
        order = await svc.create_order(
            user_id=uid,
            template_id=tid,
            input_payload={"names": ["Priya", "Arjun"], "date": "2025-12-01"},
            idempotency_key=_ikey(),
        )

    assert order.id is not None
    assert order.user_id == uid
    assert order.template_id == tid
    assert order.price_paise == 3_000
    assert order.status == OrderStatus.queued


@pytest.mark.asyncio
async def test_successful_order_debits_credits(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    tid = await _make_template(db, price_paise=4_000)
    await _credit_user(db, uid, 10_000)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        svc, _ = _svc(sess)
        await svc.create_order(
            user_id=uid,
            template_id=tid,
            input_payload={},
            idempotency_key=_ikey(),
        )

    async with factory() as sess2:
        balance = await _get_balance(sess2, uid)
    assert balance == 6_000  # 10_000 - 4_000


@pytest.mark.asyncio
async def test_successful_order_enqueues_job(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    tid = await _make_template(db, price_paise=1_000)
    await _credit_user(db, uid, 5_000)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    q = _queue()
    async with factory() as sess:
        svc = OrderService(session=sess, queue=q)
        order = await svc.create_order(
            user_id=uid,
            template_id=tid,
            input_payload={},
            idempotency_key=_ikey(),
        )

    assert len(q.enqueued) == 1
    func_name, kwargs = q.enqueued[0]
    assert func_name == "generate_content"
    assert kwargs["order_id"] == order.id


@pytest.mark.asyncio
async def test_successful_order_stores_input_payload(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    tid = await _make_template(db, price_paise=2_000)
    await _credit_user(db, uid, 5_000)
    payload = {"names": ["Rekha", "Suresh"], "theme": "royal", "language": "te"}

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        svc, _ = _svc(sess)
        order = await svc.create_order(
            user_id=uid,
            template_id=tid,
            input_payload=payload,
            idempotency_key=_ikey(),
        )

    async with factory() as sess2:
        fetched = await sess2.get(Order, order.id)
    assert fetched.input_payload == payload


# ---------------------------------------------------------------------------
# Insufficient balance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insufficient_balance_raises(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    tid = await _make_template(db, price_paise=10_000)
    await _credit_user(db, uid, 5_000)  # less than price

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        svc, _ = _svc(sess)
        with pytest.raises(InsufficientBalanceError) as exc_info:
            await svc.create_order(
                user_id=uid, template_id=tid, input_payload={}, idempotency_key=_ikey()
            )
    err = exc_info.value
    assert err.available == 5_000
    assert err.requested == 10_000


@pytest.mark.asyncio
async def test_insufficient_balance_creates_no_order_row(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    tid = await _make_template(db, price_paise=99_000)
    await _credit_user(db, uid, 100)  # way below price

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        svc, _ = _svc(sess)
        ikey = _ikey()
        try:
            await svc.create_order(
                user_id=uid, template_id=tid, input_payload={}, idempotency_key=ikey
            )
        except InsufficientBalanceError:
            pass

    async with factory() as sess2:
        result = await sess2.execute(
            select(Order).where(Order.idempotency_key == ikey)
        )
        assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_insufficient_balance_does_not_debit(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    tid = await _make_template(db, price_paise=10_000)
    await _credit_user(db, uid, 3_000)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        svc, _ = _svc(sess)
        try:
            await svc.create_order(
                user_id=uid, template_id=tid, input_payload={}, idempotency_key=_ikey()
            )
        except InsufficientBalanceError:
            pass

    async with factory() as sess2:
        balance = await _get_balance(sess2, uid)
    assert balance == 3_000  # unchanged


@pytest.mark.asyncio
async def test_insufficient_balance_enqueues_no_job(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    tid = await _make_template(db, price_paise=50_000)
    await _credit_user(db, uid, 100)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    q = _queue()
    async with factory() as sess:
        svc = OrderService(session=sess, queue=q)
        try:
            await svc.create_order(
                user_id=uid, template_id=tid, input_payload={}, idempotency_key=_ikey()
            )
        except InsufficientBalanceError:
            pass

    assert q.enqueued == []


# ---------------------------------------------------------------------------
# Template not found / inactive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_template_raises(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    await _credit_user(db, uid, 10_000)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        svc, _ = _svc(sess)
        with pytest.raises(TemplateNotFoundError):
            await svc.create_order(
                user_id=uid,
                template_id=999_999,
                input_payload={},
                idempotency_key=_ikey(),
            )


@pytest.mark.asyncio
async def test_inactive_template_raises(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    await _credit_user(db, uid, 10_000)

    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO templates (name, language, theme, asset_keys, base_price_paise, active) "
                "VALUES ('Old', 'hi', 'plain', '{}', 1000, false) RETURNING id"
            )
        )
        inactive_tid = result.scalar_one()

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        svc, _ = _svc(sess)
        with pytest.raises(TemplateNotFoundError):
            await svc.create_order(
                user_id=uid,
                template_id=inactive_tid,
                input_payload={},
                idempotency_key=_ikey(),
            )


# ---------------------------------------------------------------------------
# Double-submit idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_submit_returns_same_order(db: AsyncSession, engine) -> None:
    """Two calls with the same idempotency key must return the same order row."""
    uid = await _make_user(db)
    tid = await _make_template(db, price_paise=2_000)
    await _credit_user(db, uid, 10_000)
    ikey = _ikey()

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        svc, _ = _svc(sess)
        first = await svc.create_order(
            user_id=uid, template_id=tid, input_payload={}, idempotency_key=ikey
        )

    async with factory() as sess2:
        svc2, _ = _svc(sess2)
        second = await svc2.create_order(
            user_id=uid, template_id=tid, input_payload={}, idempotency_key=ikey
        )

    assert first.id == second.id


@pytest.mark.asyncio
async def test_double_submit_does_not_double_charge(db: AsyncSession, engine) -> None:
    """Replaying an order creation must not debit the user twice."""
    uid = await _make_user(db)
    tid = await _make_template(db, price_paise=3_000)
    await _credit_user(db, uid, 10_000)
    ikey = _ikey()

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    for _ in range(3):
        async with factory() as sess:
            svc, _ = _svc(sess)
            await svc.create_order(
                user_id=uid, template_id=tid, input_payload={}, idempotency_key=ikey
            )

    async with factory() as sess_check:
        balance = await _get_balance(sess_check, uid)
    assert balance == 7_000  # debited once: 10_000 - 3_000


@pytest.mark.asyncio
async def test_double_submit_enqueues_job_only_on_first_call(db: AsyncSession, engine) -> None:
    """The job is enqueued only on the first create_order call, not on replays."""
    uid = await _make_user(db)
    tid = await _make_template(db, price_paise=1_000)
    await _credit_user(db, uid, 5_000)
    ikey = _ikey()

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    q = LoggingJobQueue()

    async with factory() as sess:
        svc = OrderService(session=sess, queue=q)
        await svc.create_order(
            user_id=uid, template_id=tid, input_payload={}, idempotency_key=ikey
        )

    async with factory() as sess2:
        svc2 = OrderService(session=sess2, queue=q)
        await svc2.create_order(
            user_id=uid, template_id=tid, input_payload={}, idempotency_key=ikey
        )

    # Replay returns early before enqueue
    assert len(q.enqueued) == 1


# ---------------------------------------------------------------------------
# GET /orders/{id} — get_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_order_returns_correct_order(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    tid = await _make_template(db, price_paise=2_000)
    await _credit_user(db, uid, 5_000)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        svc, _ = _svc(sess)
        created = await svc.create_order(
            user_id=uid, template_id=tid, input_payload={}, idempotency_key=_ikey()
        )

    async with factory() as sess2:
        svc2, _ = _svc(sess2)
        fetched = await svc2.get_order(created.id, uid)

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.status == OrderStatus.queued


@pytest.mark.asyncio
async def test_get_order_returns_none_for_wrong_user(db: AsyncSession, engine) -> None:
    """Users can't see each other's orders."""
    uid1 = await _make_user(db)
    uid2 = await _make_user(db)
    tid = await _make_template(db, price_paise=1_000)
    await _credit_user(db, uid1, 5_000)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        svc, _ = _svc(sess)
        order = await svc.create_order(
            user_id=uid1, template_id=tid, input_payload={}, idempotency_key=_ikey()
        )

    async with factory() as sess2:
        svc2, _ = _svc(sess2)
        result = await svc2.get_order(order.id, uid2)  # wrong user
    assert result is None


@pytest.mark.asyncio
async def test_get_order_returns_none_for_missing_id(db: AsyncSession) -> None:
    uid = await _make_user(db)
    svc, _ = _svc(db)
    assert await svc.get_order(999_999, uid) is None


# ---------------------------------------------------------------------------
# Atomicity: enqueue failure does not affect committed order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_failure_does_not_rollback_order(db: AsyncSession, engine) -> None:
    """
    If the Redis enqueue fails, the order and debit are already committed.
    The order stays at status=queued for the reconciliation cron to pick up.
    """

    class FailingQueue:
        enqueued: list = []

        async def enqueue(self, function: str, **kwargs) -> None:
            raise RuntimeError("Redis is down")

    uid = await _make_user(db)
    tid = await _make_template(db, price_paise=2_000)
    await _credit_user(db, uid, 5_000)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    order_id: int
    async with factory() as sess:
        svc = OrderService(session=sess, queue=FailingQueue())
        # Should not raise — enqueue failures are swallowed after commit
        order = await svc.create_order(
            user_id=uid,
            template_id=tid,
            input_payload={},
            idempotency_key=_ikey(),
        )
        order_id = order.id

    # Order is committed and credits are debited despite the queue failure
    async with factory() as sess2:
        committed_order = await sess2.get(Order, order_id)
        balance = await _get_balance(sess2, uid)

    assert committed_order is not None
    assert committed_order.status == OrderStatus.queued
    assert balance == 3_000  # 5_000 - 2_000, debited correctly
