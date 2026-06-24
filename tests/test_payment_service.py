"""
Tests for PaymentService — order creation, webhook handling, idempotency.

Uses:
  - A StubPaymentGateway (no HTTP calls)
  - A known HMAC secret so we can forge valid/invalid signatures in tests
  - Live PostgreSQL (skipped when not reachable)

Note: CreditsService uses PostgreSQL advisory locks (no Redis dependency),
so payment tests need no Redis fixture.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.payment import GatewayOrder, PaymentGateway
from app.models.ledger import CreditLedger
from app.models.payment import Payment, PaymentStatus
from app.services.credits import CreditsService
from app.services.payment import PaymentService, WebhookSignatureError

# ---------------------------------------------------------------------------
# Database fixture
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://weddingapp:weddingapp@localhost:5432/weddingapp",
)


@pytest_asyncio.fixture()
async def engine():
    eng = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True, pool_size=5)
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
# Stub gateway
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = "test_webhook_secret_32bytes_abcdef"


class StubPaymentGateway:
    """
    Returns predictable fixture data; verifies HMAC using WEBHOOK_SECRET.
    No network calls.
    """

    def __init__(self, key_id: str = "rzp_test_KEY") -> None:
        self._key_id = key_id

    async def create_order(self, *, amount_paise: int, receipt: str) -> GatewayOrder:
        return GatewayOrder(
            gateway_order_id=f"order_{uuid.uuid4().hex[:16]}",
            amount_paise=amount_paise,
            currency="INR",
            key_id=self._key_id,
            receipt=receipt,
        )

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        expected = hmac.new(
            WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign(payload: dict) -> tuple[bytes, str]:
    """Return (raw_body_bytes, valid_signature) for a payload dict."""
    raw = json.dumps(payload).encode()
    sig = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, sig


def _captured_payload(gateway_order_id: str, gateway_payment_id: str, amount_paise: int) -> dict:
    return {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": gateway_payment_id,
                    "order_id": gateway_order_id,
                    "amount": amount_paise,
                    "status": "captured",
                }
            }
        },
    }


def _failed_payload(gateway_order_id: str, gateway_payment_id: str) -> dict:
    return {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": gateway_payment_id,
                    "order_id": gateway_order_id,
                    "amount": 0,
                    "status": "failed",
                }
            }
        },
    }


async def _make_user(db: AsyncSession) -> int:
    result = await db.execute(
        text(
            "INSERT INTO users (phone, name, preferred_language, role) "
            "VALUES (:ph, 'Test', 'hi', 'consumer') RETURNING id"
        ),
        {"ph": f"+91pay{uuid.uuid4().hex[:10]}"},
    )
    uid = result.scalar_one()
    await db.flush()
    return uid


async def _get_user(db: AsyncSession, uid: int):
    from app.models.user import User
    result = await db.execute(select(User).where(User.id == uid))
    return result.scalar_one()


def _svc(db: AsyncSession, gateway=None) -> PaymentService:
    return PaymentService(session=db, gateway=gateway or StubPaymentGateway())


async def _ledger_balance(db: AsyncSession, user_id: int) -> int:
    from sqlalchemy import func
    result = await db.execute(
        select(func.coalesce(func.sum(CreditLedger.delta_paise), 0)).where(
            CreditLedger.user_id == user_id
        )
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# create_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_order_returns_gateway_data(db: AsyncSession) -> None:
    uid = await _make_user(db)
    user = await _get_user(db, uid)
    svc = _svc(db)

    result = await svc.create_order(user=user, amount_paise=5_000)

    assert result.gateway_order.amount_paise == 5_000
    assert result.gateway_order.gateway_order_id.startswith("order_")
    assert result.gateway_order.key_id == "rzp_test_KEY"
    assert result.gateway_order.currency == "INR"


@pytest.mark.asyncio
async def test_create_order_persists_payment_row(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    user = await _get_user(db, uid)
    svc = _svc(db)

    result = await svc.create_order(user=user, amount_paise=10_000)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess2:
        payment = await sess2.get(Payment, result.payment.id)
    assert payment is not None
    assert payment.user_id == uid
    assert payment.amount_paise == 10_000
    assert payment.status == PaymentStatus.created
    assert payment.gateway_order_id == result.gateway_order.gateway_order_id


# ---------------------------------------------------------------------------
# webhook — happy path (payment captured)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_captured_webhook_credits_user(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    user = await _get_user(db, uid)

    # Create the order so the payment row exists
    svc = _svc(db)
    order_result = await svc.create_order(user=user, amount_paise=20_000)
    gateway_order_id = order_result.gateway_order.gateway_order_id
    gateway_payment_id = f"pay_{uuid.uuid4().hex[:16]}"

    # Process webhook in a fresh session (mimics real server behaviour)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess2:
        svc2 = PaymentService(session=sess2, gateway=StubPaymentGateway())
        # Inject fake redis into the embedded CreditsService
        svc2._credits = CreditsService(session=sess2)

        payload = _captured_payload(gateway_order_id, gateway_payment_id, 20_000)
        raw, sig = _sign(payload)
        await svc2.handle_webhook(raw_body=raw, signature=sig, payload=payload)

    async with factory() as sess3:
        balance = await _ledger_balance(sess3, uid)
    assert balance == 20_000


@pytest.mark.asyncio
async def test_captured_webhook_updates_payment_status(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    user = await _get_user(db, uid)
    svc = _svc(db)
    order_result = await svc.create_order(user=user, amount_paise=5_000)
    gateway_order_id = order_result.gateway_order.gateway_order_id
    payment_id = order_result.payment.id
    gateway_payment_id = f"pay_{uuid.uuid4().hex[:16]}"

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess2:
        svc2 = PaymentService(session=sess2, gateway=StubPaymentGateway())
        svc2._credits = CreditsService(session=sess2)
        payload = _captured_payload(gateway_order_id, gateway_payment_id, 5_000)
        raw, sig = _sign(payload)
        await svc2.handle_webhook(raw_body=raw, signature=sig, payload=payload)

    async with factory() as sess3:
        payment = await sess3.get(Payment, payment_id)
    assert payment.status == PaymentStatus.paid
    assert payment.gateway_payment_id == gateway_payment_id
    assert payment.raw_webhook is not None


@pytest.mark.asyncio
async def test_captured_webhook_stores_raw_body(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    user = await _get_user(db, uid)
    svc = _svc(db)
    order_result = await svc.create_order(user=user, amount_paise=3_000)
    gateway_order_id = order_result.gateway_order.gateway_order_id
    gateway_payment_id = f"pay_{uuid.uuid4().hex[:16]}"

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess2:
        svc2 = PaymentService(session=sess2, gateway=StubPaymentGateway())
        svc2._credits = CreditsService(session=sess2)
        payload = _captured_payload(gateway_order_id, gateway_payment_id, 3_000)
        raw, sig = _sign(payload)
        await svc2.handle_webhook(raw_body=raw, signature=sig, payload=payload)

    async with factory() as sess3:
        payment = await sess3.get(Payment, order_result.payment.id)
    assert payment.raw_webhook["event"] == "payment.captured"
    assert payment.raw_webhook["payload"]["payment"]["entity"]["id"] == gateway_payment_id


# ---------------------------------------------------------------------------
# webhook — idempotency (replayed webhook)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replayed_captured_webhook_does_not_double_credit(
    db: AsyncSession, engine
) -> None:
    """Delivering the same captured webhook twice must credit exactly once."""
    uid = await _make_user(db)
    user = await _get_user(db, uid)
    svc = _svc(db)
    order_result = await svc.create_order(user=user, amount_paise=8_000)
    gateway_order_id = order_result.gateway_order.gateway_order_id
    gateway_payment_id = f"pay_{uuid.uuid4().hex[:16]}"

    payload = _captured_payload(gateway_order_id, gateway_payment_id, 8_000)
    raw, sig = _sign(payload)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    for _ in range(3):  # deliver webhook three times
        async with factory() as sess:
            svc_n = PaymentService(session=sess, gateway=StubPaymentGateway())
            svc_n._credits = CreditsService(session=sess)
            await svc_n.handle_webhook(raw_body=raw, signature=sig, payload=payload)

    async with factory() as sess_check:
        balance = await _ledger_balance(sess_check, uid)
    assert balance == 8_000  # credited once, not three times


# ---------------------------------------------------------------------------
# webhook — bad signature
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_signature_raises(db: AsyncSession) -> None:
    uid = await _make_user(db)
    user = await _get_user(db, uid)
    svc = _svc(db)
    order_result = await svc.create_order(user=user, amount_paise=1_000)
    gateway_order_id = order_result.gateway_order.gateway_order_id

    payload = _captured_payload(gateway_order_id, "pay_fake", 1_000)
    raw, _ = _sign(payload)

    with pytest.raises(WebhookSignatureError):
        await svc.handle_webhook(
            raw_body=raw,
            signature="0" * 64,  # wrong signature
            payload=payload,
        )


@pytest.mark.asyncio
async def test_tampered_body_raises(db: AsyncSession) -> None:
    """Signature was computed over original body; altering a byte invalidates it."""
    uid = await _make_user(db)
    user = await _get_user(db, uid)
    svc = _svc(db)
    order_result = await svc.create_order(user=user, amount_paise=1_000)
    gateway_order_id = order_result.gateway_order.gateway_order_id

    payload = _captured_payload(gateway_order_id, "pay_real", 1_000)
    raw, sig = _sign(payload)

    # Flip one byte in the middle of the raw body.
    # We still pass the original payload dict — handle_webhook runs the HMAC
    # check on raw_body first, before using payload, so this correctly tests
    # that a body modification invalidates the signature.
    mid = len(raw) // 2
    tampered_raw = raw[:mid] + bytes([raw[mid] ^ 0xFF]) + raw[mid + 1:]

    with pytest.raises(WebhookSignatureError):
        await svc.handle_webhook(
            raw_body=tampered_raw,
            signature=sig,
            payload=payload,  # original dict — only raw_body is tampered
        )


@pytest.mark.asyncio
async def test_bad_signature_does_not_credit(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    user = await _get_user(db, uid)
    svc = _svc(db)
    order_result = await svc.create_order(user=user, amount_paise=5_000)
    gateway_order_id = order_result.gateway_order.gateway_order_id

    payload = _captured_payload(gateway_order_id, "pay_fake", 5_000)
    raw, _ = _sign(payload)

    try:
        await svc.handle_webhook(raw_body=raw, signature="bad", payload=payload)
    except WebhookSignatureError:
        pass

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess:
        balance = await _ledger_balance(sess, uid)
    assert balance == 0


# ---------------------------------------------------------------------------
# webhook — failed payment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_webhook_sets_payment_failed(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    user = await _get_user(db, uid)
    svc = _svc(db)
    order_result = await svc.create_order(user=user, amount_paise=4_000)
    gateway_order_id = order_result.gateway_order.gateway_order_id
    payment_id = order_result.payment.id
    gateway_payment_id = f"pay_{uuid.uuid4().hex[:16]}"

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess2:
        svc2 = PaymentService(session=sess2, gateway=StubPaymentGateway())
        svc2._credits = CreditsService(session=sess2)
        payload = _failed_payload(gateway_order_id, gateway_payment_id)
        raw, sig = _sign(payload)
        await svc2.handle_webhook(raw_body=raw, signature=sig, payload=payload)

    async with factory() as sess3:
        payment = await sess3.get(Payment, payment_id)
    assert payment.status == PaymentStatus.failed
    assert payment.raw_webhook["event"] == "payment.failed"


@pytest.mark.asyncio
async def test_failed_webhook_does_not_credit(db: AsyncSession, engine) -> None:
    uid = await _make_user(db)
    user = await _get_user(db, uid)
    svc = _svc(db)
    order_result = await svc.create_order(user=user, amount_paise=6_000)
    gateway_order_id = order_result.gateway_order.gateway_order_id
    gateway_payment_id = f"pay_{uuid.uuid4().hex[:16]}"

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as sess2:
        svc2 = PaymentService(session=sess2, gateway=StubPaymentGateway())
        svc2._credits = CreditsService(session=sess2)
        payload = _failed_payload(gateway_order_id, gateway_payment_id)
        raw, sig = _sign(payload)
        await svc2.handle_webhook(raw_body=raw, signature=sig, payload=payload)

    async with factory() as sess3:
        balance = await _ledger_balance(sess3, uid)
    assert balance == 0


# ---------------------------------------------------------------------------
# out-of-order: failed then captured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_capture_after_failure_credits_user(
    db: AsyncSession, engine
) -> None:
    """
    Razorpay can send payment.failed first, then payment.captured later
    (documented edge case).  The second webhook must credit the user and
    update the payment row to paid.
    """
    uid = await _make_user(db)
    user = await _get_user(db, uid)
    svc = _svc(db)
    order_result = await svc.create_order(user=user, amount_paise=7_000)
    gateway_order_id = order_result.gateway_order.gateway_order_id
    payment_id = order_result.payment.id
    gateway_payment_id = f"pay_{uuid.uuid4().hex[:16]}"

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # First: failure webhook
    async with factory() as sess2:
        svc2 = PaymentService(session=sess2, gateway=StubPaymentGateway())
        svc2._credits = CreditsService(session=sess2)
        payload = _failed_payload(gateway_order_id, gateway_payment_id)
        raw, sig = _sign(payload)
        await svc2.handle_webhook(raw_body=raw, signature=sig, payload=payload)

    # Second: late capture webhook (same payment_id, different event)
    async with factory() as sess3:
        svc3 = PaymentService(session=sess3, gateway=StubPaymentGateway())
        svc3._credits = CreditsService(session=sess3)
        payload = _captured_payload(gateway_order_id, gateway_payment_id, 7_000)
        raw, sig = _sign(payload)
        await svc3.handle_webhook(raw_body=raw, signature=sig, payload=payload)

    async with factory() as sess4:
        payment = await sess4.get(Payment, payment_id)
        balance = await _ledger_balance(sess4, uid)

    assert payment.status == PaymentStatus.paid
    assert balance == 7_000


# ---------------------------------------------------------------------------
# unknown order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_for_unknown_order_is_ignored(db: AsyncSession) -> None:
    """Webhook for an order not in our DB must not raise — just log and ack."""
    svc = _svc(db)
    payload = _captured_payload("order_unknown_xyz", "pay_xyz", 1_000)
    raw, sig = _sign(payload)
    # Must not raise
    await svc.handle_webhook(raw_body=raw, signature=sig, payload=payload)
