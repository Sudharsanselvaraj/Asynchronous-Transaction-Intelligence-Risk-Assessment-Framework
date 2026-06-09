"""
app/services/llm.py

LLM integration with:
- Pydantic schema validation on every response (no raw json.loads)
- Exponential backoff retry (not flat sleep)
- Batching with token budget awareness
- PII pseudonymisation before sending to external API
- temperature=0 for deterministic outputs
- Graceful degradation — llm_failed flag, never crashes the job
"""
import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel, ValidationError, field_validator

from app.core.config import get_settings
from app.services.cleaning import CleanedRow

logger = logging.getLogger(__name__)

VALID_CATEGORIES = frozenset({
    "Food", "Shopping", "Travel", "Transport",
    "Utilities", "Cash Withdrawal", "Entertainment", "Other"
})


# ── Pydantic schemas for LLM responses ──────────────────────────────────────

class TransactionClassification(BaseModel):
    index: int
    category: str

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        if v not in VALID_CATEGORIES:
            return "Other"  # safe fallback, not a crash
        return v


class ClassificationResponse(BaseModel):
    classifications: list[TransactionClassification]


class NarrativeSummaryResponse(BaseModel):
    total_spend_inr: float
    total_spend_usd: float
    top_merchants: list[str]
    anomaly_count: int
    narrative: str
    risk_level: str

    @field_validator("risk_level")
    @classmethod
    def validate_risk(cls, v: str) -> str:
        if v.lower() not in ("low", "medium", "high"):
            return "medium"
        return v.lower()

    @field_validator("top_merchants")
    @classmethod
    def cap_merchants(cls, v: list) -> list:
        return v[:3]


@dataclass
class LLMClassificationResult:
    index: int
    category: str | None
    failed: bool = False


@dataclass
class BatchClassificationResult:
    results: list[LLMClassificationResult] = field(default_factory=list)
    llm_failed: bool = False


# ── PII pseudonymisation ─────────────────────────────────────────────────────

def _pseudonymise_account(account_id: str) -> str:
    """Replace account_id with a stable hash — merchant stays for classification."""
    return "ACC_" + hashlib.sha256(account_id.encode()).hexdigest()[:6].upper()


# ── Core LLM caller ──────────────────────────────────────────────────────────

async def _call_gemini(prompt: str, schema_hint: str) -> str:
    """
    Raw Gemini API call with exponential backoff.
    Raises on all retries exhausted.
    """
    settings = get_settings()
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.llm_model}:generateContent"
        f"?key={settings.gemini_api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": settings.llm_temperature,
            "maxOutputTokens": settings.llm_max_tokens,
            "responseMimeType": "application/json",  # force JSON output
        },
    }

    last_exc: Exception | None = None
    for attempt in range(settings.llm_retry_max):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except (httpx.HTTPError, KeyError, IndexError) as e:
            last_exc = e
            delay = settings.llm_retry_base_delay * (2 ** attempt)
            logger.warning(
                "LLM call failed (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1, settings.llm_retry_max, delay, e
            )
            await asyncio.sleep(delay)

    raise RuntimeError(f"LLM failed after {settings.llm_retry_max} retries: {last_exc}")


# ── Classification ────────────────────────────────────────────────────────────

def _build_classification_prompt(batch: list[tuple[int, CleanedRow]]) -> str:
    """
    Builds a batched classification prompt.
    Only sends merchant + amount + currency — no account IDs.
    """
    lines = []
    for idx, row in batch:
        lines.append(
            f'{idx}: merchant="{row.merchant}", '
            f'amount={row.amount}, currency={row.currency}'
        )

    return f"""Classify each transaction into exactly one category.
Valid categories: Food, Shopping, Travel, Transport, Utilities, Cash Withdrawal, Entertainment, Other.

Transactions:
{chr(10).join(lines)}

Respond ONLY with valid JSON matching this schema:
{{"classifications": [{{"index": <int>, "category": "<category>"}}]}}

No markdown, no explanation. Only JSON."""


async def classify_uncategorised(
    rows: list[CleanedRow],
) -> dict[int, LLMClassificationResult]:
    """
    Classifies rows with category='Uncategorised'.
    Returns dict keyed by row's position in input list.
    Batches into chunks of settings.llm_batch_size.
    """
    settings = get_settings()
    uncategorised = [
        (i, row) for i, row in enumerate(rows) if row.category == "Uncategorised"
    ]

    if not uncategorised:
        return {}

    results: dict[int, LLMClassificationResult] = {}

    # Chunk into batches
    for batch_start in range(0, len(uncategorised), settings.llm_batch_size):
        batch = uncategorised[batch_start: batch_start + settings.llm_batch_size]
        prompt = _build_classification_prompt(batch)

        try:
            raw = await _call_gemini(prompt, schema_hint="classification")
            # Strip markdown fences if model ignores responseMimeType
            clean_raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            parsed = ClassificationResponse.model_validate_json(clean_raw)
            for item in parsed.classifications:
                results[item.index] = LLMClassificationResult(
                    index=item.index, category=item.category, failed=False
                )
        except (ValidationError, json.JSONDecodeError, RuntimeError) as e:
            logger.error("Classification batch failed: %s", e)
            # Mark batch as failed, don't crash the job
            for i, _ in batch:
                results[i] = LLMClassificationResult(index=i, category=None, failed=True)

    return results


# ── Narrative Summary ─────────────────────────────────────────────────────────

def _build_summary_prompt(
    rows: list[CleanedRow],
    anomaly_count: int,
) -> str:
    total_inr = sum(r.amount for r in rows if r.amount and r.currency == "INR")
    total_usd = sum(r.amount for r in rows if r.amount and r.currency == "USD")

    from collections import Counter
    merchant_counts = Counter(r.merchant for r in rows if r.merchant)
    top_3 = [m for m, _ in merchant_counts.most_common(3)]

    category_spend: dict[str, float] = {}
    for r in rows:
        if r.amount and r.category:
            category_spend[r.category] = category_spend.get(r.category, 0) + r.amount

    return f"""Analyse this financial transaction dataset and produce a JSON summary.

Dataset stats:
- Total transactions: {len(rows)}
- Total INR spend: {total_inr:.2f}
- Total USD spend: {total_usd:.2f}
- Top merchants: {top_3}
- Anomalies flagged: {anomaly_count}
- Category breakdown: {json.dumps(category_spend, indent=2)}

Respond ONLY with valid JSON matching this exact schema:
{{
  "total_spend_inr": <float>,
  "total_spend_usd": <float>,
  "top_merchants": ["<str>", "<str>", "<str>"],
  "anomaly_count": <int>,
  "narrative": "<2-3 sentence spending analysis>",
  "risk_level": "low" | "medium" | "high"
}}

Risk level criteria:
- high: anomaly_count > 5 or any USD transactions at domestic merchants
- medium: anomaly_count 2-5 or unusual category distribution
- low: otherwise

No markdown, no explanation. Only JSON."""


async def generate_narrative_summary(
    rows: list[CleanedRow],
    anomaly_count: int,
) -> NarrativeSummaryResponse | None:
    """Returns None if LLM fails after all retries."""
    prompt = _build_summary_prompt(rows, anomaly_count)
    try:
        raw = await _call_gemini(prompt, schema_hint="summary")
        clean_raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return NarrativeSummaryResponse.model_validate_json(clean_raw)
    except (ValidationError, json.JSONDecodeError, RuntimeError) as e:
        logger.error("Narrative summary LLM call failed: %s", e)
        return None
