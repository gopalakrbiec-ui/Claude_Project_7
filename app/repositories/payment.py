from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import Payment, PaymentGateway, PaymentStatus


class PaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: int,
        gateway: PaymentGateway,
        gateway_order_id: str,
        amount_paise: int,
        idempotency_key: str,
    ) -> Payment:
        payment = Payment(
            user_id=user_id,
            gateway=gateway,
            gateway_order_id=gateway_order_id,
            amount_paise=amount_paise,
            status=PaymentStatus.created,
            idempotency_key=idempotency_key,
        )
        self._session.add(payment)
        await self._session.flush()
        return payment

    async def get_by_gateway_order_id(self, gateway_order_id: str) -> Payment | None:
        result = await self._session.execute(
            select(Payment).where(Payment.gateway_order_id == gateway_order_id)
        )
        return result.scalar_one_or_none()

    async def get_by_gateway_payment_id(self, gateway_payment_id: str) -> Payment | None:
        result = await self._session.execute(
            select(Payment).where(Payment.gateway_payment_id == gateway_payment_id)
        )
        return result.scalar_one_or_none()

    async def set_paid(
        self,
        payment: Payment,
        gateway_payment_id: str,
        raw_webhook: dict,
    ) -> None:
        payment.gateway_payment_id = gateway_payment_id
        payment.status = PaymentStatus.paid
        payment.raw_webhook = raw_webhook
        await self._session.flush()

    async def set_failed(
        self,
        payment: Payment,
        gateway_payment_id: str | None,
        raw_webhook: dict,
    ) -> None:
        if gateway_payment_id:
            payment.gateway_payment_id = gateway_payment_id
        payment.status = PaymentStatus.failed
        payment.raw_webhook = raw_webhook
        await self._session.flush()
