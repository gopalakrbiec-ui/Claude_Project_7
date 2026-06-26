from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.adapters.otp import OtpAdapter
from app.models.user import User
from app.repositories.user import UserRepository
from app.services.auth import AuthService, InvalidTokenError, decode_token

import redis.asyncio as aioredis

_bearer = HTTPBearer(auto_error=True)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """
    FastAPI dependency.  Validates the Bearer JWT and returns the User row.

    Raises HTTP 401 on any token problem (missing, expired, bad signature).
    Raises HTTP 404 if the user_id in the token no longer exists in the DB
    (e.g. account deleted — rare, but handled cleanly).
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(credentials.credentials)
    except InvalidTokenError:
        raise credentials_exception

    user_id_str: str | None = payload.get("sub")
    if not user_id_str:
        raise credentials_exception

    try:
        user_id = int(user_id_str)
    except ValueError:
        raise credentials_exception

    user = await UserRepository(db).get_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return user


def get_otp_adapter() -> OtpAdapter:
    from app.adapters.otp import ConsoleOtpAdapter, Msg91OtpAdapter
    from app.core.config import get_settings
    settings = get_settings()
    if settings.otp_provider == "msg91" and settings.msg91_auth_key:
        return Msg91OtpAdapter(
            auth_key=settings.msg91_auth_key,
            template_id=settings.msg91_template_id,
            sender_id=settings.msg91_sender_id,
        )
    return ConsoleOtpAdapter()


async def get_auth_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[aioredis.Redis, Depends(get_redis)],
    otp_adapter: Annotated[OtpAdapter, Depends(get_otp_adapter)],
) -> AuthService:
    return AuthService(session=db, redis=redis, otp_adapter=otp_adapter)
