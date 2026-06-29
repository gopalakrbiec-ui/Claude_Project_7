from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderStatus


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, order_id: int) -> Order | None:
        result = await self._session.execute(
            select(Order).where(Order.id == order_id)
        )
        return result.scalar_one_or_none()

    async def get_by_idempotency_key(self, key: str) -> Order | None:
        result = await self._session.execute(
            select(Order).where(Order.idempotency_key == key)
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        user_id: int,
        template_id: int,
        input_payload: dict,
        price_paise: int,
        idempotency_key: str,
        agent_id: int | None = None,
    ) -> Order:
        order = Order(
            user_id=user_id,
            template_id=template_id,
            input_payload=input_payload,
            price_paise=price_paise,
            idempotency_key=idempotency_key,
            agent_id=agent_id,
            status=OrderStatus.queued,  # set directly to queued; debit happens first
        )
        self._session.add(order)
        await self._session.flush()  # populate order.id
        return order

    async def update_status(self, order_id: int, status: OrderStatus) -> None:
        await self._session.execute(
            update(Order).where(Order.id == order_id).values(status=status)
        )

    async def list_for_user(
        self, user_id: int, *, page: int = 1, limit: int = 20
    ) -> tuple[list[Order], int]:
        offset = (page - 1) * limit
        q = (
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        count_q = select(func.count()).select_from(Order).where(Order.user_id == user_id)
        orders = list((await self._session.execute(q)).scalars().all())
        total = (await self._session.execute(count_q)).scalar_one()
        return orders, total
