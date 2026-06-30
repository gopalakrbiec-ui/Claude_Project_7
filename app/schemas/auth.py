from __future__ import annotations

from pydantic import BaseModel, Field


class RequestOtpIn(BaseModel):
    phone: str = Field(..., min_length=10, max_length=20, examples=["+919876543210"])


class RequestOtpOut(BaseModel):
    detail: str  # "OTP sent" — never reveal whether phone exists


class VerifyOtpIn(BaseModel):
    phone: str = Field(..., min_length=10, max_length=20)
    code: str = Field(..., min_length=6, max_length=6)


class VerifyOtpOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    is_new_user: bool
    role: str  # "consumer" | "agent" | "admin"


class MeOut(BaseModel):
    id: int
    phone: str | None = None
    email: str | None = None
    name: str
    city: str | None = None
    preferred_language: str
    role: str

    model_config = {"from_attributes": True}


class TokenPayload(BaseModel):
    sub: str   # str(user_id)
    phone: str
