from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_phone(self, phone: str) -> User | None:
        result = await self._session.execute(
            select(User).where(User.phone == phone)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> User | None:
        result = await self._session.execute(
            select(User).where(User.id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        result = await self._session.execute(
            select(User).where(User.email == email)
        )
        return result.scalar_one_or_none()

    async def get_by_facebook_id(self, facebook_id: str) -> User | None:
        result = await self._session.execute(
            select(User).where(User.facebook_id == facebook_id)
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        phone: str | None = None,
        name: str = "",
        email: str | None = None,
        city: str | None = None,
        hashed_password: str | None = None,
        facebook_id: str | None = None,
        preferred_language: str = "hi",
        role: UserRole = UserRole.consumer,
    ) -> User:
        user = User(
            phone=phone,
            name=name or phone or email or "User",
            email=email,
            city=city,
            hashed_password=hashed_password,
            facebook_id=facebook_id,
            preferred_language=preferred_language,
            role=role,
        )
        self._session.add(user)
        await self._session.flush()
        return user
