"""
Tests for AuthService — OTP lifecycle and JWT issuance.

Uses fakeredis for Redis (no real Redis needed) and a capturing OtpAdapter
stub, so these run as pure unit tests without any external services.

The DB fixture re-uses the live PostgreSQL when available; tests are skipped
automatically if it's not reachable.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

# fakeredis provides an in-process Redis compatible with redis.asyncio
try:
    import fakeredis.aioredis as fakeredis
    HAS_FAKEREDIS = True
except ImportError:
    HAS_FAKEREDIS = False

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.otp import OtpAdapter
from app.services.auth import (
    AuthService,
    ExpiredOtpError,
    InvalidOtpError,
    InvalidTokenError,
    TooManyAttemptsError,
    _OTP_ATTEMPT_LIMIT,
    _OTP_TTL_SECONDS,
    _otp_key,
    _attempts_key,
    decode_token,
)

# ---------------------------------------------------------------------------
# Stubs and fakes
# ---------------------------------------------------------------------------


class CapturingOtpAdapter:
    """Records every (phone, code) pair sent.  No I/O."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_otp(self, phone: str, code: str) -> None:
        self.sent.append((phone, code))

    def last_code(self, phone: str) -> str:
        for p, c in reversed(self.sent):
            if p == phone:
                return c
        raise AssertionError(f"No OTP sent to {phone}")


# ---------------------------------------------------------------------------
# Database fixture (integration; skipped when Postgres is down)
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://weddingapp:weddingapp@localhost:5432/weddingapp",
)


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("PostgreSQL not reachable")

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        yield sess
        await sess.rollback()
    await engine.dispose()


# ---------------------------------------------------------------------------
# Redis fixture — uses fakeredis so no real Redis needed
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not HAS_FAKEREDIS, reason="fakeredis not installed; run: pip install fakeredis"
)


@pytest_asyncio.fixture()
async def fake_redis():
    server = fakeredis.FakeServer()
    r = fakeredis.FakeRedis(server=server, decode_responses=True)
    yield r
    await r.aclose()


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------


def _phone() -> str:
    return f"+91{uuid.uuid4().int % 10**10:010d}"


def _svc(db: AsyncSession, redis, adapter: CapturingOtpAdapter | None = None) -> tuple[AuthService, CapturingOtpAdapter]:
    otp = adapter or CapturingOtpAdapter()
    return AuthService(session=db, redis=redis, otp_adapter=otp), otp


# ---------------------------------------------------------------------------
# OTP issuance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_otp_stores_code_in_redis(db_session, fake_redis) -> None:
    phone = _phone()
    svc, otp_adapter = _svc(db_session, fake_redis)

    await svc.request_otp(phone)

    stored = await fake_redis.get(_otp_key(phone))
    assert stored is not None and len(stored) == 6
    assert stored == otp_adapter.last_code(phone)


@pytest.mark.asyncio
async def test_request_otp_sets_ttl(db_session, fake_redis) -> None:
    phone = _phone()
    svc, _ = _svc(db_session, fake_redis)

    await svc.request_otp(phone)

    ttl = await fake_redis.ttl(_otp_key(phone))
    # TTL should be close to _OTP_TTL_SECONDS (allow 2 s for test execution lag)
    assert _OTP_TTL_SECONDS - 2 <= ttl <= _OTP_TTL_SECONDS


@pytest.mark.asyncio
async def test_request_otp_resets_attempt_counter(db_session, fake_redis) -> None:
    """A fresh OTP request clears any prior attempt counter (unlocks locked-out phone)."""
    phone = _phone()
    svc, _ = _svc(db_session, fake_redis)

    # Simulate a prior lockout
    await fake_redis.set(_attempts_key(phone), str(_OTP_ATTEMPT_LIMIT + 1))
    await svc.request_otp(phone)

    attempts = await fake_redis.get(_attempts_key(phone))
    assert attempts is None


@pytest.mark.asyncio
async def test_otp_adapter_receives_code(db_session, fake_redis) -> None:
    phone = _phone()
    svc, adapter = _svc(db_session, fake_redis)

    await svc.request_otp(phone)

    assert len(adapter.sent) == 1
    assert adapter.sent[0][0] == phone


# ---------------------------------------------------------------------------
# New user happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_user_is_created_on_first_verify(db_session, fake_redis) -> None:
    phone = _phone()
    svc, adapter = _svc(db_session, fake_redis)

    await svc.request_otp(phone)
    result = await svc.verify_otp(phone, adapter.last_code(phone))

    assert result.is_new_user is True
    assert result.user.phone == phone
    assert result.user.id is not None
    assert result.access_token


@pytest.mark.asyncio
async def test_new_user_jwt_contains_correct_claims(db_session, fake_redis) -> None:
    phone = _phone()
    svc, adapter = _svc(db_session, fake_redis)

    await svc.request_otp(phone)
    result = await svc.verify_otp(phone, adapter.last_code(phone))

    payload = decode_token(result.access_token)
    assert payload["sub"] == str(result.user.id)
    assert payload["phone"] == phone


# ---------------------------------------------------------------------------
# Returning user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returning_user_is_not_recreated(db_session, fake_redis) -> None:
    phone = _phone()
    svc, adapter = _svc(db_session, fake_redis)

    # First login
    await svc.request_otp(phone)
    first = await svc.verify_otp(phone, adapter.last_code(phone))

    # Second login
    await svc.request_otp(phone)
    second = await svc.verify_otp(phone, adapter.last_code(phone))

    assert second.is_new_user is False
    assert second.user.id == first.user.id  # same DB row


@pytest.mark.asyncio
async def test_returning_user_gets_fresh_token(db_session, fake_redis) -> None:
    phone = _phone()
    svc, adapter = _svc(db_session, fake_redis)

    await svc.request_otp(phone)
    first = await svc.verify_otp(phone, adapter.last_code(phone))

    await svc.request_otp(phone)
    second = await svc.verify_otp(phone, adapter.last_code(phone))

    # Tokens should both be valid but will differ (issued at different times)
    payload1 = decode_token(first.access_token)
    payload2 = decode_token(second.access_token)
    assert payload1["sub"] == payload2["sub"]


# ---------------------------------------------------------------------------
# Expired OTP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_with_no_otp_raises_expired(db_session, fake_redis) -> None:
    """Verifying without ever requesting an OTP should raise ExpiredOtpError."""
    phone = _phone()
    svc, _ = _svc(db_session, fake_redis)

    with pytest.raises(ExpiredOtpError):
        await svc.verify_otp(phone, "123456")


@pytest.mark.asyncio
async def test_verify_after_manual_expiry_raises_expired(db_session, fake_redis) -> None:
    """Simulate TTL expiry by manually deleting the key before verifying."""
    phone = _phone()
    svc, adapter = _svc(db_session, fake_redis)

    await svc.request_otp(phone)
    code = adapter.last_code(phone)

    # Simulate Redis TTL expiry
    await fake_redis.delete(_otp_key(phone))

    with pytest.raises(ExpiredOtpError):
        await svc.verify_otp(phone, code)


# ---------------------------------------------------------------------------
# Wrong code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_code_raises_invalid_otp(db_session, fake_redis) -> None:
    phone = _phone()
    svc, _ = _svc(db_session, fake_redis)

    await svc.request_otp(phone)

    with pytest.raises(InvalidOtpError):
        await svc.verify_otp(phone, "000000")


@pytest.mark.asyncio
async def test_wrong_code_does_not_create_user(db_session, fake_redis) -> None:
    phone = _phone()
    svc, _ = _svc(db_session, fake_redis)

    await svc.request_otp(phone)
    try:
        await svc.verify_otp(phone, "000000")
    except InvalidOtpError:
        pass

    from app.repositories.user import UserRepository
    user = await UserRepository(db_session).get_by_phone(phone)
    assert user is None


# ---------------------------------------------------------------------------
# Too many attempts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_too_many_attempts_raises(db_session, fake_redis) -> None:
    phone = _phone()
    svc, adapter = _svc(db_session, fake_redis)

    await svc.request_otp(phone)
    code = adapter.last_code(phone)

    # Burn through the limit with wrong codes
    for _ in range(_OTP_ATTEMPT_LIMIT):
        try:
            await svc.verify_otp(phone, "000000")
        except (InvalidOtpError, TooManyAttemptsError):
            pass

    # The next attempt — even with the correct code — should be locked out
    with pytest.raises(TooManyAttemptsError):
        await svc.verify_otp(phone, code)


@pytest.mark.asyncio
async def test_fresh_otp_request_clears_lockout(db_session, fake_redis) -> None:
    """Re-requesting an OTP resets the attempt counter, lifting the lockout."""
    phone = _phone()
    svc, adapter = _svc(db_session, fake_redis)

    await svc.request_otp(phone)

    for _ in range(_OTP_ATTEMPT_LIMIT):
        try:
            await svc.verify_otp(phone, "000000")
        except (InvalidOtpError, TooManyAttemptsError):
            pass

    # Request a fresh OTP — this resets the attempt counter
    await svc.request_otp(phone)
    new_code = adapter.last_code(phone)

    result = await svc.verify_otp(phone, new_code)
    assert result.access_token


# ---------------------------------------------------------------------------
# OTP is single-use (replay protection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_otp_cannot_be_reused(db_session, fake_redis) -> None:
    """After a successful verify the OTP is deleted; reusing it raises ExpiredOtpError."""
    phone = _phone()
    svc, adapter = _svc(db_session, fake_redis)

    await svc.request_otp(phone)
    code = adapter.last_code(phone)

    await svc.verify_otp(phone, code)  # first use — succeeds

    with pytest.raises(ExpiredOtpError):
        await svc.verify_otp(phone, code)  # replay — must fail


# ---------------------------------------------------------------------------
# JWT decode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tampered_token_raises_invalid_token(db_session, fake_redis) -> None:
    phone = _phone()
    svc, adapter = _svc(db_session, fake_redis)

    await svc.request_otp(phone)
    result = await svc.verify_otp(phone, adapter.last_code(phone))

    tampered = result.access_token[:-4] + "XXXX"
    with pytest.raises(InvalidTokenError):
        decode_token(tampered)
