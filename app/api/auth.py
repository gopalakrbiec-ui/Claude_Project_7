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


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _pwd_context():
    from passlib.context import CryptContext
    return CryptContext(schemes=["bcrypt"], deprecated="auto")


def _hash_password(password: str) -> str:
    return _pwd_context().hash(password)


def _verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context().verify(plain, hashed)


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
    return _token_response(user, is_new=True)


# ---------------------------------------------------------------------------
# POST /auth/google  — Google Sign-In (id_token from Flutter)
# ---------------------------------------------------------------------------

class GoogleAuthRequest(BaseModel):
    id_token: str


@router.post("/google", summary="Sign in with Google")
async def google_auth(
    body: GoogleAuthRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from app.core.config import get_settings
    settings = get_settings()

    if not settings.google_client_id:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google auth not configured")

    # Verify id_token with Google (sync call — run in thread)
    import asyncio
    try:
        info = await asyncio.get_event_loop().run_in_executor(
            None, _verify_google_token, body.id_token, settings.google_client_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid Google token: {exc}")

    email: str = info["email"]
    name: str = info.get("name", "")

    from app.repositories.user import UserRepository
    repo = UserRepository(db)

    user = await repo.get_by_email(email)
    is_new = user is None
    if is_new:
        user = await repo.create(name=name, email=email)
        await db.commit()

    return _token_response(user, is_new=is_new)


def _verify_google_token(id_token_str: str, client_id: str) -> dict:
    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests
    return id_token.verify_oauth2_token(id_token_str, google_requests.Request(), client_id)


# ---------------------------------------------------------------------------
# POST /auth/facebook  — Facebook Login (access_token from Flutter)
# ---------------------------------------------------------------------------

class FacebookAuthRequest(BaseModel):
    access_token: str


@router.post("/facebook", summary="Sign in with Facebook")
async def facebook_auth(
    body: FacebookAuthRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://graph.facebook.com/me",
            params={"fields": "id,name,email", "access_token": body.access_token},
        )

    if r.status_code != 200:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Facebook token")

    data = r.json()
    if "error" in data:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Facebook token")

    fb_id: str = data["id"]
    name: str = data.get("name", "")
    email: str | None = data.get("email")

    from app.repositories.user import UserRepository
    repo = UserRepository(db)

    user = await repo.get_by_facebook_id(fb_id)
    if user is None and email:
        user = await repo.get_by_email(email)

    is_new = user is None
    if is_new:
        user = await repo.create(name=name, email=email, facebook_id=fb_id)
        await db.commit()
    elif user.facebook_id is None:
        user.facebook_id = fb_id
        await db.commit()

    return _token_response(user, is_new=is_new)


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

    return VerifyOtpOut(
        access_token=result.access_token,
        is_new_user=result.is_new_user,
        role=result.user.role.value,
    )


@router.get("/me", response_model=MeOut, status_code=status.HTTP_200_OK)
async def get_me(
    current_user: Annotated[User, Depends(get_current_user)],
) -> MeOut:
    """Return the authenticated user's profile."""
    return MeOut.model_validate(current_user)
