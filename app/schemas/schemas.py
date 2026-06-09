"""
app/schemas/schemas.py

Pydantic v2 request/response models.
Separates API contract from ORM models.
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ── Standard error envelope ───────────────────────────────────────────────────

class ErrorDetail(BaseModel):
    code: str
    message: str
    field: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
    request_id: str | None = None


# ── Job schemas ───────────────────────────────────────────────────────────────

class JobCreatedResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    message: str


class JobSummarySchema(BaseModel):
    total_spend_inr: float
    total_spend_usd: float
    top_merchants: list[str]
    category_breakdown: dict[str, float]
    anomaly_count: int
    narrative: str | None
    risk_level: str | None
    llm_failed: bool


class JobStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: uuid.UUID = Field(alias="id")
    status: str
    filename: str = Field(alias="original_filename")
    row_count_raw: int | None
    row_count_clean: int | None
    created_at: datetime
    completed_at: datetime | None
    error_message: str | None
    summary: JobSummarySchema | None = None


class JobListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: uuid.UUID = Field(alias="id")
    status: str
    filename: str = Field(alias="original_filename")
    row_count_raw: int | None
    created_at: datetime


class JobListResponse(BaseModel):
    items: list[JobListItem]
    total: int
    limit: int
    offset: int


# ── Transaction schemas ───────────────────────────────────────────────────────

class TransactionSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    txn_id: str | None
    date: str | None
    merchant: str | None
    amount: float | None
    currency: str | None
    status: str | None
    category: str | None
    account_id: str | None
    is_anomaly: bool
    anomaly_reason: str | None
    llm_category: str | None
    llm_failed: bool


class AnomalySchema(BaseModel):
    txn_id: str | None
    merchant: str | None
    amount: float | None
    currency: str | None
    reason: str


class CategoryBreakdown(BaseModel):
    category: str
    total_spend: float
    transaction_count: int


class JobResultsResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    transactions: list[TransactionSchema]
    anomalies: list[AnomalySchema]
    category_breakdown: list[CategoryBreakdown]
    summary: JobSummarySchema | None
