from __future__ import annotations

import random
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.otp import OtpAdapter
from app.core.config import get_settings
from app.models.user import User
from app.repositories.user import UserRepository

import redis.asyncio as aioredis

# ---------------------------------------------------------------------------
# Redis key helpers
# ---------------------------------------------------------------------------

_OTP_TTL_SECONDS = 300          # 5 minutes — long enough for SMS delivery lag
_OTP_ATTEMPT_LIMIT = 5          # lock out after 5 wrong guesses
_OTP_CODE_LENGTH = 6

# JWT lifetime: 30 days. Mobile apps in low-connectivity regions should not
# force re-auth frequently; losing connectivity mid-session or reinstalling
# shouldn't invalidate a session after a single day. 30 days is the practical
# floor for this market. Use short-lived tokens + refresh if you add a
# server-side session-revocation requirement later.
_JWT_LIFETIME_DAYS = 30

_ALGORITHM = "HS256"


def _otp_key(phone: str) -> str:
    return f"otp:{phone}"


def _attempts_key(phone: str) -> str:
    return f"otp_attempts:{phone}"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InvalidOtpError(Exception):
    """Wrong code supplied."""


class ExpiredOtpError(Exception):
    """OTP not found in Redis (never issued or TTL elapsed)."""


class TooManyAttemptsError(Exception):
    """Attempt counter exceeded the limit."""


class InvalidTokenError(Exception):
    """JWT is missing, malformed, expired, or signed with wrong key."""


# ---------------------------------------------------------------------------
# OTP helpers
# ---------------------------------------------------------------------------


def _generate_code() -> str:
    return "".join(random.choices(string.digits, k=_OTP_CODE_LENGTH))


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def _create_access_token(user_id: int, phone: str) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "phone": phone,
        "iat": now,
        "exp": now + timedelta(days=_JWT_LIFETIME_DAYS),
    }
    return jwt.encode(payload, settings.app_secret_key, algorithm=_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT.  Raises InvalidTokenError on any failure."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.app_secret_key, algorithms=[_ALGORITHM])
        return payload
    except JWTError as exc:
        raise InvalidTokenError(str(exc)) from exc


# ---------------------------------------------------------------------------
# AuthService
# ---------------------------------------------------------------------------


@dataclass
class VerifyResult:
    user: User
    access_token: str
    is_new_user: bool


class AuthService:
    """
    Orchestrates OTP issuance and verification.

    Dependencies are injected so tests can pass fakes without touching global
    state or environment variables.
    """

    def __init__(
        self,
        session: AsyncSession,
        redis: aioredis.Redis,
        otp_adapter: OtpAdapter,
    ) -> None:
        self._session = session
        self._redis = redis
        self._otp_adapter = otp_adapter
        self._user_repo = UserRepository(session)

    async def request_otp(self, phone: str) -> None:
        """
        Generate and deliver an OTP for the given phone number.

        Overwrites any existing OTP for that phone, resetting the TTL and
        the attempt counter.  This lets users request a fresh code if the
        first one doesn't arrive.
        """
        code = _generate_code()

        pipe = self._redis.pipeline()
        pipe.set(_otp_key(phone), code, ex=_OTP_TTL_SECONDS)
        # Reset attempt counter together with the new code so a fresh request
        # clears a locked-out phone without requiring a manual admin action.
        pipe.delete(_attempts_key(phone))
        await pipe.execute()

        await self._otp_adapter.send_otp(phone, code)

    async def verify_otp(self, phone: str, code: str) -> VerifyResult:
        """
        Verify the OTP and return a JWT + user record.

        Attempt counting uses Redis INCR so it is atomic even under concurrent
        verification requests.  The attempt key shares the OTP TTL — both
        expire together, so a lockout is automatically lifted when the OTP
        would have expired anyway.
        """
        # Increment attempt counter atomically BEFORE reading the code.
        # This prevents a race where two parallel requests both read attempt=4
        # and both proceed to check the code.
        attempts = await self._redis.incr(_attempts_key(phone))
        if attempts == 1:
            # First attempt for this code window — set TTL on the counter.
            await self._redis.expire(_attempts_key(phone), _OTP_TTL_SECONDS)

        if attempts > _OTP_ATTEMPT_LIMIT:
            raise TooManyAttemptsError(
                f"Too many OTP attempts for {phone}. Request a new code."
            )

        stored_code = await self._redis.get(_otp_key(phone))
        if stored_code is None:
            raise ExpiredOtpError(f"No active OTP for {phone}. Request a new code.")

        if stored_code != code:
            raise InvalidOtpError("Incorrect OTP code.")

        # Code is correct — delete both keys immediately so the code cannot
        # be reused (replay protection).
        pipe = self._redis.pipeline()
        pipe.delete(_otp_key(phone))
        pipe.delete(_attempts_key(phone))
        await pipe.execute()

        # Upsert user.
        user = await self._user_repo.get_by_phone(phone)
        is_new = user is None
        if user is None:
            user = await self._user_repo.create(phone=phone)
            await self._session.commit()

        token = _create_access_token(user.id, user.phone)
        return VerifyResult(user=user, access_token=token, is_new_user=is_new)
