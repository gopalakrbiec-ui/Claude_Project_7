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

    async def create(
        self,
        *,
        phone: str,
        name: str = "",
        preferred_language: str = "hi",
        role: UserRole = UserRole.consumer,
    ) -> User:
        user = User(
            phone=phone,
            name=name or phone,  # placeholder name until user fills profile
            preferred_language=preferred_language,
            role=role,
        )
        self._session.add(user)
        await self._session.flush()
        return user
