"""
Tests for the Arq generation worker pipeline.

Uses fake adapters — no real Anthropic or generation API calls.
Uses live PostgreSQL (skipped when not reachable).

Tests:
  - Clean end-to-end run with fake adapters → order status=done
  - Blocked moderation → order status=rejected, credits refunded (idempotent)
  - Terminal order skip (already done) → worker is a no-op
  - Retry safety: refund idempotency key prevents double refund
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.claude import FakeClaudeAdapter
from app.adapters.generation import FakeGenerationAdapter
from app.adapters.moderation import FakeModerationAdapter
from app.adapters.storage import FakeStorageAdapter
from app.models.generation_job import JobStatus
from app.models.ledger import LedgerReason
from app.models.order import Order, OrderStatus
from app.repositories.generation_job import GenerationJobRepository
from app.repositories.order import OrderRepository
from app.services.credits import CreditsService, LedgerRef
from app.workers.jobs import generate_content
from app.workers.pipeline import run_moderation

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


async def _make_user(db: AsyncSession) -> int:
    result = await db.execute(
        text(
            "INSERT INTO users (phone, name, preferred_language, role) "
            "VALUES (:ph, 'Pipe Test', 'hi', 'consumer') RETURNING id"
        ),
        {"ph": f"+91pipe{uuid.uuid4().hex[:10]}"},
    )
    uid = result.scalar_one()
    await db.flush()
    return uid


async def _make_template(db: AsyncSession, price_paise: int = 5_000) -> int:
    result = await db.execute(
        text(
            "INSERT INTO templates (name, language, theme, asset_keys, base_price_paise, active) "
            "VALUES ('Pipeline Test Template', 'hi', 'floral', '{}', :price, true) RETURNING id"
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
    price_paise: int = 5_000,
    status: OrderStatus = OrderStatus.queued,
    input_payload: dict | None = None,
) -> Order:
    payload = input_payload or {"theme": "floral", "language": "hi", "names": "Raj & Priya"}
    import json

    result = await db.execute(
        text(
            "INSERT INTO orders "
            "(user_id, template_id, input_payload, price_paise, idempotency_key, status) "
            "VALUES (:uid, :tid, cast(:payload as jsonb), :price, :ikey, cast(:status as order_status)) RETURNING id"
        ),
        {
            "uid": user_id,
            "tid": template_id,
            "payload": json.dumps(payload),
            "price": price_paise,
            "ikey": uuid.uuid4().hex,
            "status": status.value,
        },
    )
    order_id = result.scalar_one()
    await db.flush()

    order_repo = OrderRepository(db)
    order = await order_repo.get_by_id(order_id)
    assert order is not None
    return order


async def _credit_user(db: AsyncSession, user_id: int, amount: int) -> None:
    svc = CreditsService(db)
    await svc.credit(
        user_id=user_id,
        delta_paise=amount,
        reason=LedgerReason.purchase,
        idempotency_key=f"test:credit:{uuid.uuid4().hex}",
    )
    await db.commit()


def _make_ctx(db: AsyncSession, **overrides) -> dict:
    ctx: dict = {
        "session": db,
        "moderation_adapter": FakeModerationAdapter(),
        "claude_adapter": FakeClaudeAdapter(),
        "generation_provider": FakeGenerationAdapter(),
        "storage_adapter": FakeStorageAdapter(),
    }
    ctx.update(overrides)
    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_clean_pipeline_end_to_end(db: AsyncSession):
    """A normal order flows through all 5 steps and ends with status=done."""
    user_id = await _make_user(db)
    template_id = await _make_template(db)
    await _credit_user(db, user_id, 10_000)

    order = await _make_order(db, user_id, template_id)
    ctx = _make_ctx(db)

    await generate_content(ctx, order_id=order.id)

    # Re-fetch from DB to check final state
    order_repo = OrderRepository(db)
    updated_order = await order_repo.get_by_id(order.id)
    assert updated_order is not None
    assert updated_order.status == OrderStatus.done

    job_repo = GenerationJobRepository(db)
    job = await job_repo.get_by_order_id(order.id)
    assert job is not None
    assert job.status == JobStatus.done
    assert job.output_keys is not None
    assert "image" in job.output_keys
    assert job.provider == "fake"
    assert job.cost_paise == FakeGenerationAdapter.COST_PAISE

    # Storage adapter should hold the uploaded bytes
    storage: FakeStorageAdapter = ctx["storage_adapter"]
    assert len(storage.store) == 1
    uploaded_key = f"orders/{order.id}/output.png"
    assert uploaded_key in storage.store


async def test_blocked_order_is_rejected_and_refunded(db: AsyncSession):
    """
    A request containing __BLOCK__ is caught by moderation:
      - order status = rejected
      - credits are refunded to the user
    """
    user_id = await _make_user(db)
    template_id = await _make_template(db, price_paise=3_000)
    await _credit_user(db, user_id, 10_000)

    order = await _make_order(
        db,
        user_id,
        template_id,
        price_paise=3_000,
        input_payload={"theme": "__BLOCK__ celebrity face", "language": "hi"},
    )

    # Debit the user to simulate what OrderService.create_order does
    credits_svc = CreditsService(db)
    from app.models.ledger import LedgerReason as LR
    await credits_svc.debit(
        user_id=user_id,
        delta_paise=3_000,
        reason=LR.spend,
        ref=LedgerRef(ref_type="order", ref_id=order.id),
        idempotency_key=f"debit:order:{order.id}",
    )
    await db.commit()

    balance_post_debit = await credits_svc.get_balance(user_id)  # 7,000

    ctx = _make_ctx(db)
    await generate_content(ctx, order_id=order.id)

    order_repo = OrderRepository(db)
    updated = await order_repo.get_by_id(order.id)
    assert updated is not None
    assert updated.status == OrderStatus.rejected

    job_repo = GenerationJobRepository(db)
    job = await job_repo.get_by_order_id(order.id)
    assert job is not None
    assert job.status == JobStatus.rejected
    assert job.moderation_result is not None

    # Credits should be refunded — balance back to what it was before the debit
    balance_after = await credits_svc.get_balance(user_id)
    assert balance_after == balance_post_debit + 3_000  # refund restored the debit


async def test_blocked_refund_is_idempotent(db: AsyncSession):
    """
    If the worker retries after a crash, run_moderation is called again with the
    same refund idempotency key — the second credit call must be a no-op.
    """
    user_id = await _make_user(db)
    template_id = await _make_template(db, price_paise=2_000)
    await _credit_user(db, user_id, 10_000)

    order = await _make_order(
        db,
        user_id,
        template_id,
        price_paise=2_000,
        input_payload={"theme": "__BLOCK__ NSFW content", "language": "hi"},
    )

    job_repo = GenerationJobRepository(db)
    job = await job_repo.create(order_id=order.id)
    await db.commit()

    credits_svc = CreditsService(db)
    balance_before = await credits_svc.get_balance(user_id)

    # First moderation call — refunds and rejects
    await run_moderation(
        order, job.id, session=db, moderation_adapter=FakeModerationAdapter()
    )
    balance_mid = await credits_svc.get_balance(user_id)
    assert balance_mid == balance_before + 2_000  # refunded

    # Re-fetch order (status changed to rejected)
    order_repo = OrderRepository(db)
    order = await order_repo.get_by_id(order.id)

    # Second moderation call (simulated retry) — refund must be a no-op
    job2 = await job_repo.create(order_id=order.id)
    await db.flush()
    await run_moderation(
        order, job2.id, session=db, moderation_adapter=FakeModerationAdapter()
    )
    balance_after = await credits_svc.get_balance(user_id)
    # Balance must not change on second call — idempotency_key collision → no-op credit
    assert balance_after == balance_mid


async def test_terminal_order_is_skipped(db: AsyncSession):
    """Worker called on an already-done order is a no-op (no DB mutations)."""
    user_id = await _make_user(db)
    template_id = await _make_template(db)
    await _credit_user(db, user_id, 10_000)

    order = await _make_order(db, user_id, template_id, status=OrderStatus.done)
    ctx = _make_ctx(db)

    # Should return without touching the job row
    await generate_content(ctx, order_id=order.id)

    job_repo = GenerationJobRepository(db)
    job = await job_repo.get_by_order_id(order.id)
    # No job row should have been created
    assert job is None


async def test_nonexistent_order_is_skipped(db: AsyncSession):
    """Worker called for a missing order_id logs an error and returns cleanly."""
    ctx = _make_ctx(db)
    # Should not raise
    await generate_content(ctx, order_id=999_999_999)


async def test_storage_receives_watermarked_bytes(db: AsyncSession):
    """The fake storage adapter captures the bytes uploaded during run_upload."""
    user_id = await _make_user(db)
    template_id = await _make_template(db)
    await _credit_user(db, user_id, 10_000)

    order = await _make_order(db, user_id, template_id)
    storage = FakeStorageAdapter()
    ctx = _make_ctx(db, storage_adapter=storage)

    await generate_content(ctx, order_id=order.id)

    assert len(storage.store) == 1
    key = list(storage.store.keys())[0]
    assert key.startswith(f"orders/{order.id}/")
    assert len(storage.store[key]) > 0
