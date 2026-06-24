from __future__ import annotations

from pydantic import BaseModel, Field


class RequestOtpIn(BaseModel):
    phone: str = Field(..., min_length=10, max_length=20, examples=["+919876543210"])


class RequestOtpOut(BaseModel):
    detail: str  # "OTP sent" — never reveal whether phone exists


class VerifyOtpIn(BaseModel):
    phone: str = Field(..., min_length=10, max_length=20)
    code: str = Field(..., min_length=4, max_length=8)


class VerifyOtpOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    is_new_user: bool


class TokenPayload(BaseModel):
    sub: str   # str(user_id)
    phone: str
