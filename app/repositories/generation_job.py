from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.generation_job import GenerationJob, JobStatus


class GenerationJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, order_id: int) -> GenerationJob:
        job = GenerationJob(order_id=order_id, status=JobStatus.pending)
        self._session.add(job)
        await self._session.flush()
        return job

    async def get_by_order_id(self, order_id: int) -> GenerationJob | None:
        result = await self._session.execute(
            select(GenerationJob).where(GenerationJob.order_id == order_id)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, job_id: int) -> GenerationJob | None:
        result = await self._session.execute(
            select(GenerationJob).where(GenerationJob.id == job_id)
        )
        return result.scalar_one_or_none()

    async def update_status(self, job_id: int, status: JobStatus) -> None:
        await self._session.execute(
            update(GenerationJob).where(GenerationJob.id == job_id).values(status=status)
        )

    async def set_moderation_result(self, job_id: int, moderation_result: dict) -> None:
        await self._session.execute(
            update(GenerationJob)
            .where(GenerationJob.id == job_id)
            .values(moderation_result=moderation_result)
        )

    async def set_result(
        self,
        job_id: int,
        *,
        status: JobStatus,
        output_keys: dict | None = None,
        provider: str | None = None,
        cost_paise: int | None = None,
        error: str | None = None,
    ) -> None:
        values: dict = {"status": status}
        if output_keys is not None:
            values["output_keys"] = output_keys
        if provider is not None:
            values["provider"] = provider
        if cost_paise is not None:
            values["cost_paise"] = cost_paise
        if error is not None:
            values["error"] = error
        await self._session.execute(
            update(GenerationJob).where(GenerationJob.id == job_id).values(**values)
        )
