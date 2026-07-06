from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_auth_service, get_current_user, get_db
from app.models.user import User
from app.schemas.auth import MeOut, RequestOtpIn, RequestOtpOut, VerifyOtpIn, VerifyOtpOut
from app.services.auth import (
    AuthService,
    ExpiredOtpError,
    InvalidOtpError,
    TooManyAttemptsError,
    _create_access_token,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _hash_password(password: str) -> str:
    import bcrypt
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    import bcrypt
    return bcrypt.checkpw(plain.encode(), hashed.encode())


async def _grant_signup_bonus(db: AsyncSession, user: User) -> None:
    """
    Credit a one-time signup bonus to a newly created user.
    Idempotent via a per-user key — safe even if called twice.
    """
    from app.core.config import get_settings
    from app.models.ledger import LedgerReason
    from app.services.credits import CreditsService, LedgerRef

    settings = get_settings()
    if settings.signup_bonus_paise <= 0:
        return

    await CreditsService(db).credit(
        user_id=user.id,
        delta_paise=settings.signup_bonus_paise,
        reason=LedgerReason.signup_bonus,
        ref=LedgerRef(ref_type="signup", ref_id=user.id),
        idempotency_key=f"signup_bonus:{user.id}",
    )
    await db.commit()


def _token_response(user: User, is_new: bool) -> dict:
    identifier = user.phone or user.email or str(user.id)
    return {
        "access_token": _create_access_token(user.id, identifier),
        "token_type": "bearer",
        "role": user.role.value,
        "is_new_user": is_new,
    }


# ---------------------------------------------------------------------------
# POST /auth/login  — email or phone + password
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    identifier: str   # email OR mobile number
    password: str


@router.post("/login", summary="Login with email/phone + password")
async def login(
    body: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from app.repositories.user import UserRepository
    repo = UserRepository(db)

    user = await repo.get_by_email(body.identifier)
    if user is None:
        user = await repo.get_by_phone(body.identifier)
    if user is None or not user.hashed_password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not _verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    return _token_response(user, is_new=False)


# ---------------------------------------------------------------------------
# POST /auth/register  — email + password sign-up
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    name: str
    email: EmailStr
    mobile: str | None = None
    city: str | None = None
    password: str


@router.post("/register", status_code=status.HTTP_201_CREATED, summary="Register with email + password")
async def register(
    body: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from app.repositories.user import UserRepository
    repo = UserRepository(db)

    if await repo.get_by_email(str(body.email)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    if body.mobile and await repo.get_by_phone(body.mobile):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Mobile already registered")

    user = await repo.create(
        name=body.name,
        email=str(body.email),
        phone=body.mobile or None,
        city=body.city,
        hashed_password=_hash_password(body.password),
    )
    await db.commit()
    await _grant_signup_bonus(db, user)
    return _token_response(user, is_new=True)


# ---------------------------------------------------------------------------
# OTP flow (existing)
# ---------------------------------------------------------------------------

@router.post("/request-otp", response_model=RequestOtpOut, status_code=status.HTTP_200_OK)
async def request_otp(
    body: RequestOtpIn,
    svc: Annotated[AuthService, Depends(get_auth_service)],
) -> RequestOtpOut:
    await svc.request_otp(body.phone)
    return RequestOtpOut(detail="OTP sent")


@router.post("/verify-otp", response_model=VerifyOtpOut, status_code=status.HTTP_200_OK)
async def verify_otp(
    body: VerifyOtpIn,
    svc: Annotated[AuthService, Depends(get_auth_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VerifyOtpOut:
    """Verify OTP and return a JWT. Creates the user account on first login."""
    try:
        result = await svc.verify_otp(body.phone, body.code)
    except TooManyAttemptsError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    except ExpiredOtpError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except InvalidOtpError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    if result.is_new_user:
        await _grant_signup_bonus(db, result.user)

    return VerifyOtpOut(
        access_token=result.access_token,
        is_new_user=result.is_new_user,
        role=result.user.role.value,
    )


# ---------------------------------------------------------------------------
# POST /auth/forgot-password
# ---------------------------------------------------------------------------

class ForgotPasswordIn(BaseModel):
    email: EmailStr


@router.post("/forgot-password", status_code=status.HTTP_200_OK, summary="Request a password reset link")
async def forgot_password(
    body: ForgotPasswordIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """
    Always returns the same response regardless of whether the email exists —
    prevents user enumeration. If the email is registered, sends a reset link.
    """
    from app.core.config import get_settings
    from app.core.redis import get_redis
    from app.adapters.email import EmailAdapter
    from app.repositories.user import UserRepository
    import secrets
    import logging as _logging

    settings = get_settings()
    repo = UserRepository(db)
    user = await repo.get_by_email(str(body.email))

    if user is not None:
        token = secrets.token_urlsafe(32)
        redis = get_redis()
        await redis.set(
            f"pwd_reset:{token}",
            str(user.id),
            ex=settings.password_reset_ttl_seconds,
        )

        # Deep-link opens the app's reset screen; Flutter handles savinenapu:// scheme
        reset_link = f"savinenapu://reset-password?token={token}"

        html = f"""
        <div style="font-family:system-ui,sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;">
          <h2 style="color:#1A1916;margin-bottom:8px;">Reset your password</h2>
          <p style="color:#5C5A55;margin-bottom:24px;">Hi {user.name}, tap the button below to set a new password for your Savi Nenapu account. This link expires in 15 minutes.</p>
          <a href="{reset_link}" style="display:inline-block;background:#E87C1A;color:#fff;padding:14px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:15px;">Reset Password</a>
          <p style="color:#9B9890;font-size:13px;margin-top:24px;">If you didn't request this, you can safely ignore this email.</p>
        </div>
        """
        text = (
            f"Hi {user.name},\n\n"
            "Reset your Savi Nenapu password using this link (expires in 15 minutes):\n"
            f"{reset_link}\n\n"
            "If you didn't request this, ignore this email.\n— Savi Nenapu Team"
        )

        adapter = EmailAdapter(
            resend_api_key=settings.resend_api_key,
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
            smtp_user=settings.smtp_user,
            smtp_password=settings.smtp_password,
            from_address=settings.email_from_address,
        )
        try:
            await adapter.send(
                to=str(body.email),
                subject="Reset your Savi Nenapu password",
                html=html,
                text=text,
            )
        except Exception:
            _logging.getLogger(__name__).exception("Failed to send password reset email to %s", body.email)

    # Same response whether email exists or not — prevents user enumeration
    return {"detail": "If that email is registered, a reset link has been sent."}


# ---------------------------------------------------------------------------
# POST /auth/reset-password
# ---------------------------------------------------------------------------

class ResetPasswordIn(BaseModel):
    token: str
    new_password: str


@router.post("/reset-password", status_code=status.HTTP_200_OK, summary="Set new password using reset token")
async def reset_password(
    body: ResetPasswordIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Validates the one-time reset token and updates the user's password."""
    from app.core.redis import get_redis
    from app.repositories.user import UserRepository

    redis = get_redis()
    redis_key = f"pwd_reset:{body.token}"
    user_id_raw = await redis.get(redis_key)

    if user_id_raw is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset link is invalid or has expired. Please request a new one.",
        )

    user_id = int(user_id_raw)
    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.hashed_password = _hash_password(body.new_password)
    db.add(user)
    await db.commit()

    # Invalidate token so it can only be used once
    await redis.delete(redis_key)

    return {"detail": "Password updated successfully. You can now log in."}


@router.get("/me", response_model=MeOut, status_code=status.HTTP_200_OK)
async def get_me(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MeOut:
    """Return the authenticated user's profile including current wallet balance."""
    from app.services.credits import CreditsService
    credits_paise = await CreditsService(db).get_balance(current_user.id)
    out = MeOut.model_validate(current_user)
    out.credits_paise = credits_paise
    return out
