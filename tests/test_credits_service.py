"""
Tests for CreditsService + LedgerRepository.

Integration tests against real PostgreSQL — skipped automatically if the DB is
not reachable, so the unit-test suite (make test without Docker) stays green.

Concurrency tests use asyncio.gather() to fire simultaneous coroutines against
the same user, each in its own DB connection/transaction.  This exercises the
advisory-lock serialisation path for real.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.ledger import LedgerReason
from app.services.credits import CreditsService, InsufficientBalanceError, LedgerRef

# ---------------------------------------------------------------------------
# Database plumbing
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://weddingapp:weddingapp@localhost:5432/weddingapp",
)


def _make_engine():
    return create_async_engine(DATABASE_URL, echo=False, pool_size=10, max_overflow=10)


@pytest_asyncio.fixture()
async def engine():
    eng = _make_engine()
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await eng.dispose()
        pytest.skip("PostgreSQL not reachable — skipping credits service tests")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture()
async def session(engine):
    """Single session for simple sequential tests.  Closed (auto-rollback) after each test."""
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        yield sess
        # asyncpg rolls back any open transaction on connection return;
        # explicit rollback here ensures the connection is free before user_id
        # fixture teardown runs its DELETE (avoids lock contention).
        await sess.rollback()


@pytest_asyncio.fixture()
async def user_id(engine) -> int:
    """
    Insert a fresh user for each test and return its id.

    Uses a unique phone per test so rows never collide between parallel runs.
    No teardown DELETE — the test DB is ephemeral and random phones don't repeat.
    The concurrent-test helpers commit their own data; the session fixture
    rolls back sequential-test data before this fixture exits.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        phone = f"+91test{uuid.uuid4().hex[:12]}"
        result = await sess.execute(
            text(
                "INSERT INTO users (phone, name, preferred_language, role) "
                "VALUES (:ph, 'Test', 'hi', 'consumer') RETURNING id"
            ),
            {"ph": phone},
        )
        uid = result.scalar_one()
        await sess.commit()
    return uid


def _svc(session: AsyncSession) -> CreditsService:
    return CreditsService(session)


def _ikey() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Helper: run a mutation in its own isolated connection + commit
# ---------------------------------------------------------------------------

async def _credit_isolated(engine, user_id: int, amount: int, ikey: str) -> int:
    """Credit and commit in a fresh connection.  Returns new balance."""
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        svc = CreditsService(sess)
        await svc.credit(
            user_id=user_id,
            delta_paise=amount,
            reason=LedgerReason.purchase,
            idempotency_key=ikey,
        )
        await sess.commit()
        return await svc.get_balance(user_id)


async def _debit_isolated(
    engine,
    user_id: int,
    amount: int,
    ikey: str,
) -> int | InsufficientBalanceError:
    """
    Debit and commit in a fresh connection.
    Returns the balance after success, or the exception on insufficient funds.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        svc = CreditsService(sess)
        try:
            await svc.debit(
                user_id=user_id,
                delta_paise=amount,
                reason=LedgerReason.spend,
                idempotency_key=ikey,
            )
            await sess.commit()
            return await svc.get_balance(user_id)
        except InsufficientBalanceError as e:
            await sess.rollback()
            return e


# ---------------------------------------------------------------------------
# Basic correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_balance_is_zero(session: AsyncSession, user_id: int) -> None:
    balance = await _svc(session).get_balance(user_id)
    assert balance == 0


@pytest.mark.asyncio
async def test_credit_increases_balance(session: AsyncSession, user_id: int) -> None:
    svc = _svc(session)
    await svc.credit(
        user_id=user_id,
        delta_paise=10_000,
        reason=LedgerReason.purchase,
        idempotency_key=_ikey(),
    )
    assert await svc.get_balance(user_id) == 10_000


@pytest.mark.asyncio
async def test_debit_decreases_balance(session: AsyncSession, user_id: int) -> None:
    svc = _svc(session)
    await svc.credit(
        user_id=user_id, delta_paise=10_000, reason=LedgerReason.purchase, idempotency_key=_ikey()
    )
    await svc.debit(
        user_id=user_id, delta_paise=3_000, reason=LedgerReason.spend, idempotency_key=_ikey()
    )
    assert await svc.get_balance(user_id) == 7_000


@pytest.mark.asyncio
async def test_debit_to_exact_zero_is_allowed(session: AsyncSession, user_id: int) -> None:
    svc = _svc(session)
    await svc.credit(
        user_id=user_id, delta_paise=5_000, reason=LedgerReason.purchase, idempotency_key=_ikey()
    )
    await svc.debit(
        user_id=user_id, delta_paise=5_000, reason=LedgerReason.spend, idempotency_key=_ikey()
    )
    assert await svc.get_balance(user_id) == 0


@pytest.mark.asyncio
async def test_ledger_entry_stores_ref(session: AsyncSession, user_id: int) -> None:
    svc = _svc(session)
    ref = LedgerRef(ref_type="payment", ref_id=42)
    entry = await svc.credit(
        user_id=user_id,
        delta_paise=1_000,
        reason=LedgerReason.purchase,
        ref=ref,
        idempotency_key=_ikey(),
    )
    assert entry.ref_type == "payment"
    assert entry.ref_id == 42


@pytest.mark.asyncio
async def test_credit_rejects_non_positive_delta(session: AsyncSession, user_id: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        await _svc(session).credit(
            user_id=user_id,
            delta_paise=0,
            reason=LedgerReason.purchase,
            idempotency_key=_ikey(),
        )


@pytest.mark.asyncio
async def test_debit_rejects_non_positive_delta(session: AsyncSession, user_id: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        await _svc(session).debit(
            user_id=user_id,
            delta_paise=-100,
            reason=LedgerReason.spend,
            idempotency_key=_ikey(),
        )


# ---------------------------------------------------------------------------
# Insufficient balance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debit_insufficient_balance_raises(session: AsyncSession, user_id: int) -> None:
    svc = _svc(session)
    await svc.credit(
        user_id=user_id, delta_paise=500, reason=LedgerReason.purchase, idempotency_key=_ikey()
    )
    with pytest.raises(InsufficientBalanceError) as exc_info:
        await svc.debit(
            user_id=user_id, delta_paise=501, reason=LedgerReason.spend, idempotency_key=_ikey()
        )
    err = exc_info.value
    assert err.available == 500
    assert err.requested == 501


@pytest.mark.asyncio
async def test_debit_on_empty_balance_raises(session: AsyncSession, user_id: int) -> None:
    with pytest.raises(InsufficientBalanceError):
        await _svc(session).debit(
            user_id=user_id, delta_paise=1, reason=LedgerReason.spend, idempotency_key=_ikey()
        )


@pytest.mark.asyncio
async def test_failed_debit_does_not_alter_balance(session: AsyncSession, user_id: int) -> None:
    svc = _svc(session)
    await svc.credit(
        user_id=user_id, delta_paise=200, reason=LedgerReason.purchase, idempotency_key=_ikey()
    )
    try:
        await svc.debit(
            user_id=user_id, delta_paise=999, reason=LedgerReason.spend, idempotency_key=_ikey()
        )
    except InsufficientBalanceError:
        pass
    assert await svc.get_balance(user_id) == 200


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credit_replay_is_noop(session: AsyncSession, user_id: int) -> None:
    """Calling credit twice with the same key must not double-credit."""
    svc = _svc(session)
    key = _ikey()
    entry1 = await svc.credit(
        user_id=user_id, delta_paise=3_000, reason=LedgerReason.purchase, idempotency_key=key
    )
    entry2 = await svc.credit(
        user_id=user_id, delta_paise=3_000, reason=LedgerReason.purchase, idempotency_key=key
    )
    assert entry1.id == entry2.id
    assert await svc.get_balance(user_id) == 3_000  # not 6000


@pytest.mark.asyncio
async def test_debit_replay_is_noop(session: AsyncSession, user_id: int) -> None:
    """Replaying a debit with the same key must not double-debit."""
    svc = _svc(session)
    await svc.credit(
        user_id=user_id, delta_paise=10_000, reason=LedgerReason.purchase, idempotency_key=_ikey()
    )
    key = _ikey()
    entry1 = await svc.debit(
        user_id=user_id, delta_paise=2_000, reason=LedgerReason.spend, idempotency_key=key
    )
    entry2 = await svc.debit(
        user_id=user_id, delta_paise=2_000, reason=LedgerReason.spend, idempotency_key=key
    )
    assert entry1.id == entry2.id
    assert await svc.get_balance(user_id) == 8_000  # not 6000


@pytest.mark.asyncio
async def test_idempotency_key_collision_across_users_is_noop(engine, user_id: int) -> None:
    """
    Idempotency keys are globally unique in the ledger (no per-user scoping),
    so callers MUST supply globally unique keys (e.g. UUIDs prefixed with the
    operation context).

    When a key collision occurs across users, the service treats it as a
    duplicate and returns the EXISTING entry (no-op) rather than raising.
    This means the second user's balance is NOT credited — a silent no-op.
    This is the documented contract: key reuse across users is a caller bug.
    """
    shared_key = _ikey()
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # Create a second user.
    async with factory() as setup_sess:
        result = await setup_sess.execute(
            text(
                "INSERT INTO users (phone, name, preferred_language, role) "
                "VALUES (:ph, 'Other', 'hi', 'consumer') RETURNING id"
            ),
            {"ph": f"+91other{uuid.uuid4().hex[:10]}"},
        )
        second_uid = result.scalar_one()
        await setup_sess.commit()

    # First user credits — succeeds and writes the key.
    async with factory() as sess:
        await CreditsService(sess).credit(
            user_id=user_id,
            delta_paise=1_000,
            reason=LedgerReason.purchase,
            idempotency_key=shared_key,
        )
        await sess.commit()

    # Second user with same key — service detects existing key, returns no-op.
    async with factory() as sess2:
        entry = await CreditsService(sess2).credit(
            user_id=second_uid,
            delta_paise=500,
            reason=LedgerReason.purchase,
            idempotency_key=shared_key,
        )
        await sess2.commit()
        # The returned entry belongs to the first user (the original writer).
        assert entry.user_id == user_id
        # The second user's balance is still zero — no double-credit.
        second_balance = await CreditsService(sess2).get_balance(second_uid)
        assert second_balance == 0


# ---------------------------------------------------------------------------
# Concurrency: advisory-lock serialisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_debits_only_one_succeeds(engine, user_id: int) -> None:
    """
    Fund a user with 5000 paise, then fire 5 concurrent debits of 3000 each.
    Exactly 1 should succeed; the other 4 should raise InsufficientBalanceError.
    The final balance must be exactly 2000 paise (never negative).
    """
    await _credit_isolated(engine, user_id, 5_000, _ikey())

    tasks = [_debit_isolated(engine, user_id, 3_000, _ikey()) for _ in range(5)]
    results = await asyncio.gather(*tasks)

    successes = [r for r in results if isinstance(r, int)]
    failures = [r for r in results if isinstance(r, InsufficientBalanceError)]

    assert len(successes) == 1, f"Expected exactly 1 successful debit, got {len(successes)}"
    assert len(failures) == 4
    assert successes[0] == 2_000, f"Balance after debit should be 2000, got {successes[0]}"


@pytest.mark.asyncio
async def test_concurrent_credits_all_succeed(engine, user_id: int) -> None:
    """
    Credits don't have a balance guard, so all concurrent credits must land.
    10 × 1000 paise = 10 000 paise final balance.
    """
    tasks = [_credit_isolated(engine, user_id, 1_000, _ikey()) for _ in range(10)]
    await asyncio.gather(*tasks)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        balance = await CreditsService(sess).get_balance(user_id)
    assert balance == 10_000


@pytest.mark.asyncio
async def test_concurrent_debit_replay_does_not_double_debit(engine, user_id: int) -> None:
    """
    Simulate a worker crashing and retrying: 10 concurrent calls with the SAME
    idempotency key must result in exactly one ledger entry and one debit.
    """
    await _credit_isolated(engine, user_id, 10_000, _ikey())
    shared_key = _ikey()

    tasks = [_debit_isolated(engine, user_id, 1_000, shared_key) for _ in range(10)]
    results = await asyncio.gather(*tasks)

    # All calls that didn't fail with InsufficientBalance return a balance int.
    balances = [r for r in results if isinstance(r, int)]

    # All concurrent calls return a consistent balance (9000 after single debit).
    assert all(b == 9_000 for b in balances), f"Unexpected balances: {balances}"

    # Verify only one row was inserted.
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        from sqlalchemy import select, func
        from app.models.ledger import CreditLedger

        result = await sess.execute(
            select(func.count()).where(
                CreditLedger.user_id == user_id,
                CreditLedger.delta_paise < 0,
            )
        )
        debit_count = result.scalar_one()
    assert debit_count == 1, f"Expected 1 debit row, found {debit_count}"


@pytest.mark.asyncio
async def test_balance_never_goes_negative_under_concurrency(engine, user_id: int) -> None:
    """
    Stress test: fund with 10 000 paise, fire 20 concurrent debits of 1000.
    Exactly 10 should succeed; final balance must be 0, never negative.
    """
    await _credit_isolated(engine, user_id, 10_000, _ikey())

    tasks = [_debit_isolated(engine, user_id, 1_000, _ikey()) for _ in range(20)]
    results = await asyncio.gather(*tasks)

    successes = [r for r in results if isinstance(r, int)]
    failures = [r for r in results if isinstance(r, InsufficientBalanceError)]

    assert len(successes) == 10, f"Expected 10 successes, got {len(successes)}"
    assert len(failures) == 10
    assert all(b >= 0 for b in successes), f"Balance went negative: {successes}"

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        final = await CreditsService(sess).get_balance(user_id)
    assert final == 0
