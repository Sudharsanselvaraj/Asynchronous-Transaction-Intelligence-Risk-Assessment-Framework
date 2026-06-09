"""
app/api/routers/jobs.py

All /jobs/* endpoints.

KEY PATTERNS:
- File upload: stream to disk, never hold in RAM
- UUID validation at router level (not manual string checks)
- Correct HTTP status codes (201, 202, 404)
- Standard error envelope via HTTPException + global handler
- No business logic — delegates to repository
- Pagination on list endpoint
"""
import hashlib
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.config import get_settings
from app.db.repository import JobRepository, TransactionRepository
from app.models.models import JobStatus
from app.schemas.schemas import (
    AnomalySchema,
    CategoryBreakdown,
    JobCreatedResponse,
    JobListResponse,
    JobResultsResponse,
    JobStatusResponse,
    JobSummarySchema,
    TransactionSchema,
)
from app.workers.tasks import process_csv_task

router = APIRouter(prefix="/jobs", tags=["jobs"])


# ── POST /jobs/upload ─────────────────────────────────────────────────────────

@router.post(
    "/upload",
    response_model=JobCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a CSV file for processing",
)
async def upload_csv(
    file: Annotated[UploadFile, File(description="CSV file of transactions")],
    session: AsyncSession = Depends(get_db),
):
    settings = get_settings()

    # ── Validate MIME type ────────────────────────────────────────────
    if file.content_type not in settings.allowed_mime_types:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "INVALID_MIME_TYPE",
                "message": f"Expected CSV, got {file.content_type}",
            },
        )

    # ── Validate filename (no path traversal) ─────────────────────────
    safe_name = Path(file.filename or "upload.csv").name
    if not safe_name.lower().endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_EXTENSION", "message": "File must be a .csv"},
        )

    # ── Stream to disk with size limit ───────────────────────────────
    # Never load the entire file into RAM
    dest = settings.upload_dir / f"{uuid.uuid4().hex}_{safe_name}"
    hasher = hashlib.sha256()
    bytes_written = 0

    try:
        with open(dest, "wb") as f:
            while chunk := await file.read(65536):  # 64KB chunks
                bytes_written += len(chunk)
                if bytes_written > settings.max_upload_bytes:
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail={
                            "code": "FILE_TOO_LARGE",
                            "message": f"File exceeds {settings.max_upload_bytes // 1024 // 1024}MB limit",
                        },
                    )
                hasher.update(chunk)
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail={"code": "UPLOAD_FAILED", "message": str(e)})

    file_hash = hasher.hexdigest()

    # ── Deduplication check ───────────────────────────────────────────
    job_repo = JobRepository(session)
    existing = await job_repo.find_by_hash(file_hash)
    if existing and existing.status in (JobStatus.COMPLETED, JobStatus.PROCESSING):
        dest.unlink(missing_ok=True)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "job_id": str(existing.id),
                "status": existing.status.value,
                "message": "Duplicate file — returning existing job",
            },
        )

    # ── Create job record ─────────────────────────────────────────────
    job = await job_repo.create(
        filename=str(dest),
        original_filename=safe_name,
        file_hash=file_hash,
    )
    await session.commit()

    # ── Enqueue Celery task ───────────────────────────────────────────
    task = process_csv_task.apply_async(
        args=[str(job.id), str(dest)],
        queue="llm",
        task_id=f"job-{job.id}",  # deterministic task ID for deduplication
    )

    # Update task ID on job record
    await job_repo.set_processing(job.id, task.id)
    await session.commit()

    return JobCreatedResponse(
        job_id=job.id,
        status="pending",
        message="Job enqueued. Poll /jobs/{job_id}/status for updates.",
    )


# ── GET /jobs/{job_id}/status ─────────────────────────────────────────────────

@router.get(
    "/{job_id}/status",
    response_model=JobStatusResponse,
    summary="Poll job status",
)
async def get_job_status(
    job_id: uuid.UUID,  # FastAPI validates UUID format automatically
    session: AsyncSession = Depends(get_db),
):
    repo = JobRepository(session)
    job = await repo.get_with_summary(job_id)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "JOB_NOT_FOUND", "message": f"Job {job_id} not found"},
        )

    response = JobStatusResponse(
        id=job.id,
        status=job.status.value,
        original_filename=job.original_filename,
        row_count_raw=job.row_count_raw,
        row_count_clean=job.row_count_clean,
        created_at=job.created_at,
        completed_at=job.completed_at,
        error_message=job.error_message,
    )

    if job.status == JobStatus.COMPLETED and job.summary:
        s = job.summary
        response.summary = JobSummarySchema(
            total_spend_inr=s.total_spend_inr,
            total_spend_usd=s.total_spend_usd,
            top_merchants=s.top_merchants or [],
            category_breakdown=s.category_breakdown or {},
            anomaly_count=s.anomaly_count,
            narrative=s.narrative,
            risk_level=s.risk_level,
            llm_failed=s.llm_failed,
        )

    return response


# ── GET /jobs/{job_id}/results ────────────────────────────────────────────────

@router.get(
    "/{job_id}/results",
    response_model=JobResultsResponse,
    summary="Get full results for a completed job",
)
async def get_job_results(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
):
    repo = JobRepository(session)
    job = await repo.get_with_transactions(job_id)

    if not job:
        raise HTTPException(status_code=404, detail={"code": "JOB_NOT_FOUND", "message": "Not found"})

    if job.status != JobStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "JOB_NOT_COMPLETED",
                "message": f"Job is {job.status.value}, results not yet available",
            },
        )

    transactions = [TransactionSchema.model_validate(t) for t in job.transactions]
    anomalies = [
        AnomalySchema(
            txn_id=t.txn_id,
            merchant=t.merchant,
            amount=t.amount,
            currency=t.currency,
            reason=t.anomaly_reason or "",
        )
        for t in job.transactions if t.is_anomaly
    ]

    # Category breakdown
    cat_map: dict[str, dict] = {}
    for t in job.transactions:
        cat = t.category or "Uncategorised"
        if cat not in cat_map:
            cat_map[cat] = {"total_spend": 0.0, "transaction_count": 0}
        cat_map[cat]["total_spend"] += t.amount or 0.0
        cat_map[cat]["transaction_count"] += 1

    category_breakdown = [
        CategoryBreakdown(category=k, **v)
        for k, v in sorted(cat_map.items(), key=lambda x: -x[1]["total_spend"])
    ]

    summary = None
    if job.summary:
        s = job.summary
        summary = JobSummarySchema(
            total_spend_inr=s.total_spend_inr,
            total_spend_usd=s.total_spend_usd,
            top_merchants=s.top_merchants or [],
            category_breakdown=s.category_breakdown or {},
            anomaly_count=s.anomaly_count,
            narrative=s.narrative,
            risk_level=s.risk_level,
            llm_failed=s.llm_failed,
        )

    return JobResultsResponse(
        job_id=job.id,
        status=job.status.value,
        transactions=transactions,
        anomalies=anomalies,
        category_breakdown=category_breakdown,
        summary=summary,
    )


# ── GET /jobs ─────────────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=JobListResponse,
    summary="List all jobs with optional status filter",
)
async def list_jobs(
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: AsyncSession = Depends(get_db),
):
    repo = JobRepository(session)
    jobs = await repo.list_jobs(status=status_filter, limit=limit, offset=offset)
    return JobListResponse(
        items=jobs,
        total=len(jobs),  # in production: add COUNT(*) query
        limit=limit,
        offset=offset,
    )
