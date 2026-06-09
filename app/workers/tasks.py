"""
app/workers/tasks.py

Celery tasks are thin orchestrators — they call service functions.
Business logic lives in services/, not here.

KEY PATTERNS:
- self.update_state() for progress visibility
- asyncio.run() to call async service functions from sync Celery context
- NullPool engine for DB (forked process safety)
- Task deduplication via task_id == job_id
- Cleanup beat task to prevent disk fill
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.models import JobSummary, Transaction
from app.services.anomaly import detect_anomalies
from app.services.cleaning import clean_csv
from app.services.llm import classify_uncategorised, generate_narrative_summary
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _get_worker_session() -> async_sessionmaker:
    """Lazy init of worker DB session using NullPool engine."""
    from app.db.session import get_worker_engine
    engine = get_worker_engine()
    return async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False
    )


@celery_app.task(
    bind=True,
    max_retries=0,  # we handle retries internally per-step
    task_id_from_args=False,
    name="app.workers.tasks.process_csv_task",
)
def process_csv_task(self: Task, job_id: str, file_path: str) -> dict:
    """
    Orchestrates the full processing pipeline for one job.
    Returns summary dict on success.
    """
    return asyncio.run(_process_csv_async(self, job_id, file_path))


async def _process_csv_async(task: Task, job_id: str, file_path: str) -> dict:
    job_uuid = uuid.UUID(job_id)
    path = Path(file_path)
    SessionLocal = _get_worker_session()

    async with SessionLocal() as session:
        # Import here to avoid circular imports at module load time
        from app.db.repository import JobRepository, SummaryRepository, TransactionRepository

        job_repo = JobRepository(session)
        txn_repo = TransactionRepository(session)
        sum_repo = SummaryRepository(session)

        try:
            # ── Step 1: Mark as processing ────────────────────────────
            task.update_state(state="PROGRESS", meta={"step": "cleaning", "pct": 10})
            await job_repo.set_processing(job_uuid, task.request.id)
            await session.commit()

            # ── Step 2: Clean ─────────────────────────────────────────
            cleaning_result = clean_csv(path)
            logger.info(
                "Job %s cleaned: %d raw → %d clean (%d dupes removed)",
                job_id, cleaning_result.raw_count,
                cleaning_result.clean_count, cleaning_result.duplicate_count
            )

            # ── Step 3: Anomaly detection ─────────────────────────────
            task.update_state(state="PROGRESS", meta={"step": "anomaly_detection", "pct": 40})
            anomaly_results = detect_anomalies(
                cleaning_result.rows,
                multiplier=3.0,
            )
            anomaly_count = sum(1 for r in anomaly_results if r.is_anomaly)

            # ── Step 4: LLM classification ────────────────────────────
            task.update_state(state="PROGRESS", meta={"step": "llm_classification", "pct": 60})
            classifications = await classify_uncategorised(cleaning_result.rows)

            # ── Step 5: Build Transaction records ─────────────────────
            task.update_state(state="PROGRESS", meta={"step": "persisting", "pct": 80})
            db_transactions = []
            for i, (row, anomaly) in enumerate(zip(cleaning_result.rows, anomaly_results)):
                cls_result = classifications.get(i)
                final_category = row.category
                llm_category = None
                llm_failed = False

                if row.category == "Uncategorised" and cls_result:
                    if not cls_result.failed and cls_result.category:
                        final_category = cls_result.category
                        llm_category = cls_result.category
                    else:
                        llm_failed = True

                db_transactions.append(
                    Transaction(
                        job_id=job_uuid,
                        txn_id=row.txn_id,
                        date=row.date,
                        merchant=row.merchant,
                        amount=row.amount,
                        currency=row.currency,
                        status=row.status,
                        category=final_category,
                        account_id=row.account_id,
                        notes=row.notes,
                        is_anomaly=anomaly.is_anomaly,
                        anomaly_reason=anomaly.reason,
                        llm_category=llm_category,
                        llm_failed=llm_failed,
                    )
                )

            await txn_repo.bulk_insert(db_transactions)

            # ── Step 6: Narrative summary ─────────────────────────────
            task.update_state(state="PROGRESS", meta={"step": "narrative", "pct": 90})
            narrative = await generate_narrative_summary(
                cleaning_result.rows, anomaly_count
            )

            # Build category breakdown
            category_spend: dict[str, float] = {}
            for row in cleaning_result.rows:
                if row.amount and row.category:
                    category_spend[row.category] = (
                        category_spend.get(row.category, 0) + row.amount
                    )

            summary = JobSummary(
                job_id=job_uuid,
                total_spend_inr=narrative.total_spend_inr if narrative else 0.0,
                total_spend_usd=narrative.total_spend_usd if narrative else 0.0,
                top_merchants=narrative.top_merchants if narrative else [],
                category_breakdown=category_spend,
                anomaly_count=anomaly_count,
                narrative=narrative.narrative if narrative else None,
                risk_level=narrative.risk_level if narrative else None,
                llm_failed=narrative is None,
            )
            await sum_repo.upsert(summary)

            # ── Step 7: Mark completed ────────────────────────────────
            await job_repo.set_completed(
                job_uuid,
                row_count_raw=cleaning_result.raw_count,
                row_count_clean=cleaning_result.clean_count,
            )
            await session.commit()

            # Clean up uploaded file after successful processing
            path.unlink(missing_ok=True)

            logger.info("Job %s completed successfully", job_id)
            return {"job_id": job_id, "status": "completed", "anomaly_count": anomaly_count}

        except SoftTimeLimitExceeded:
            await session.rollback()
            await job_repo.set_failed(job_uuid, "Task exceeded 5-minute soft time limit")
            await session.commit()
            raise

        except Exception as e:
            await session.rollback()
            logger.exception("Job %s failed: %s", job_id, e)
            try:
                async with SessionLocal() as err_session:
                    err_repo = JobRepository(err_session)
                    await err_repo.set_failed(job_uuid, str(e))
                    await err_session.commit()
            except Exception:
                logger.exception("Failed to mark job %s as failed", job_id)
            raise


@celery_app.task(name="app.workers.tasks.cleanup_old_uploads_task")
def cleanup_old_uploads_task() -> dict:
    """Beat task: delete orphaned upload files older than 2 hours."""
    from app.core.config import get_settings
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    deleted = 0
    for f in settings.upload_dir.glob("*.csv"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            f.unlink(missing_ok=True)
            deleted += 1
    logger.info("Cleanup: deleted %d orphaned upload files", deleted)
    return {"deleted": deleted}
