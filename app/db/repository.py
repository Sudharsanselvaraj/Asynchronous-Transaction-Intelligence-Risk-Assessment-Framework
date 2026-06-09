"""
app/db/repository.py

All database queries live here. Routes and services are DB-agnostic.
Uses async SQLAlchemy — all queries are non-blocking.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.models import Job, JobStatus, JobSummary, Transaction


class JobRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        *,
        filename: str,
        original_filename: str,
        file_hash: str,
    ) -> Job:
        job = Job(
            filename=filename,
            original_filename=original_filename,
            file_hash=file_hash,
            status=JobStatus.PENDING,
        )
        self.session.add(job)
        await self.session.flush()  # gets ID without committing — caller commits
        return job

    async def get_by_id(self, job_id: uuid.UUID) -> Job | None:
        result = await self.session.execute(
            select(Job).where(Job.id == job_id)
        )
        return result.scalar_one_or_none()

    async def get_with_summary(self, job_id: uuid.UUID) -> Job | None:
        result = await self.session.execute(
            select(Job)
            .options(selectinload(Job.summary))
            .where(Job.id == job_id)
        )
        return result.scalar_one_or_none()

    async def get_with_transactions(self, job_id: uuid.UUID) -> Job | None:
        result = await self.session.execute(
            select(Job)
            .options(selectinload(Job.transactions), selectinload(Job.summary))
            .where(Job.id == job_id)
        )
        return result.scalar_one_or_none()

    async def list_jobs(
        self,
        *,
        status: JobStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Job]:
        q = select(Job).order_by(Job.created_at.desc()).limit(limit).offset(offset)
        if status:
            q = q.where(Job.status == status)
        result = await self.session.execute(q)
        return list(result.scalars().all())

    async def set_processing(self, job_id: uuid.UUID, celery_task_id: str) -> None:
        await self.session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(status=JobStatus.PROCESSING, celery_task_id=celery_task_id)
        )

    async def set_completed(
        self, job_id: uuid.UUID, row_count_raw: int, row_count_clean: int
    ) -> None:
        await self.session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.COMPLETED,
                row_count_raw=row_count_raw,
                row_count_clean=row_count_clean,
                completed_at=datetime.now(timezone.utc),
            )
        )

    async def set_failed(self, job_id: uuid.UUID, error: str) -> None:
        await self.session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.FAILED,
                error_message=error[:2000],  # cap at 2000 chars
                completed_at=datetime.now(timezone.utc),
            )
        )

    async def find_by_hash(self, file_hash: str) -> Job | None:
        """Deduplication: find existing job with same file content."""
        result = await self.session.execute(
            select(Job)
            .where(Job.file_hash == file_hash)
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


class TransactionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def bulk_insert(self, transactions: list[Transaction]) -> None:
        """Single-commit batch insert — not 95 individual commits."""
        self.session.add_all(transactions)
        await self.session.flush()

    async def get_by_job(self, job_id: uuid.UUID) -> list[Transaction]:
        result = await self.session.execute(
            select(Transaction)
            .where(Transaction.job_id == job_id)
            .order_by(Transaction.created_at)
        )
        return list(result.scalars().all())


class SummaryRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(self, summary: JobSummary) -> None:
        self.session.add(summary)
        await self.session.flush()
