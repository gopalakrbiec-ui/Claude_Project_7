from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_auth_service, get_current_user
from app.models.user import User
from app.schemas.auth import MeOut, RequestOtpIn, RequestOtpOut, VerifyOtpIn, VerifyOtpOut
from app.services.auth import (
    AuthService,
    ExpiredOtpError,
    InvalidOtpError,
    TooManyAttemptsError,
)

router = APIRouter(prefix="/auth", tags=["auth"])


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
