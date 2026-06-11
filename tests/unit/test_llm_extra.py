"""
tests/unit/test_llm_extra.py

Extended LLM service tests covering:
- generate_narrative_summary (happy path + failure)
- Risk level coercion
- top_merchants cap at 3
- Markdown fence stripping in classify_uncategorised
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.cleaning import CleanedRow
from app.services.llm import NarrativeSummaryResponse, classify_uncategorised, generate_narrative_summary


def _row(merchant: str, amount: float = 1000.0, currency: str = "INR", category: str = "Food") -> CleanedRow:
    return CleanedRow(
        txn_id="T",
        date="2024-01-01",
        merchant=merchant,
        amount=amount,
        currency=currency,
        status="SUCCESS",
        category=category,
        account_id="ACC001",
        notes=None,
        raw_index=0,
    )


# ── generate_narrative_summary ────────────────────────────────────────────────

class TestNarrativeSummary:
    @pytest.mark.asyncio
    async def test_returns_parsed_response_on_success(self):
        payload = {
            "total_spend_inr": 50_000.0,
            "total_spend_usd": 500.0,
            "top_merchants": ["Flipkart", "Swiggy", "Ola"],
            "anomaly_count": 3,
            "narrative": "Spend is dominated by e-commerce and food delivery.",
            "risk_level": "medium",
        }
        with patch("app.services.llm._call_gemini", new=AsyncMock(return_value=json.dumps(payload))):
            result = await generate_narrative_summary(
                [_row("Flipkart", 50_000.0), _row("Swiggy", 500.0, "USD")],
                anomaly_count=3,
            )
        assert isinstance(result, NarrativeSummaryResponse)
        assert result.total_spend_inr == 50_000.0
        assert result.total_spend_usd == 500.0
        assert result.risk_level == "medium"
        assert result.anomaly_count == 3

    @pytest.mark.asyncio
    async def test_returns_none_on_llm_failure(self):
        with patch("app.services.llm._call_gemini", new=AsyncMock(side_effect=RuntimeError("API down"))):
            result = await generate_narrative_summary([_row("Flipkart")], anomaly_count=0)
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_risk_level_coerced_to_medium(self):
        payload = {
            "total_spend_inr": 100.0,
            "total_spend_usd": 0.0,
            "top_merchants": ["Flipkart"],
            "anomaly_count": 0,
            "narrative": "Normal spending.",
            "risk_level": "critical",  # not in {low, medium, high}
        }
        with patch("app.services.llm._call_gemini", new=AsyncMock(return_value=json.dumps(payload))):
            result = await generate_narrative_summary([_row("Flipkart")], anomaly_count=0)
        assert result.risk_level == "medium"

    @pytest.mark.asyncio
    async def test_top_merchants_capped_at_3(self):
        payload = {
            "total_spend_inr": 100.0,
            "total_spend_usd": 0.0,
            "top_merchants": ["A", "B", "C", "D", "E"],  # validator should cap at 3
            "anomaly_count": 0,
            "narrative": "Normal.",
            "risk_level": "low",
        }
        with patch("app.services.llm._call_gemini", new=AsyncMock(return_value=json.dumps(payload))):
            result = await generate_narrative_summary([_row("A")], anomaly_count=0)
        assert len(result.top_merchants) == 3

    @pytest.mark.asyncio
    async def test_returns_none_on_invalid_json(self):
        with patch("app.services.llm._call_gemini", new=AsyncMock(return_value="not json at all")):
            result = await generate_narrative_summary([_row("Flipkart")], anomaly_count=0)
        assert result is None


# ── classify_uncategorised — additional cases ─────────────────────────────────

class TestClassificationExtra:
    @pytest.mark.asyncio
    async def test_markdown_fence_stripped_before_parse(self):
        """Model sometimes wraps JSON in ```json ... ``` despite responseMimeType."""
        inner = json.dumps({
            "classifications": [{"index": 0, "category": "Food"}]
        })
        fenced = f"```json\n{inner}\n```"
        with patch("app.services.llm._call_gemini", new=AsyncMock(return_value=fenced)):
            rows = [CleanedRow(
                txn_id="T", date="2024-01-01", merchant="Swiggy",
                amount=100.0, currency="INR", status="SUCCESS",
                category="Uncategorised", account_id="ACC001", notes=None, raw_index=0,
            )]
            result = await classify_uncategorised(rows)
        assert result[0].category == "Food"
        assert not result[0].failed

    @pytest.mark.asyncio
    async def test_already_categorised_rows_not_sent_to_llm(self):
        """Rows that already have a category must be skipped entirely — no LLM call."""
        rows = [_row("Swiggy", category="Food")]  # already categorised
        with patch("app.services.llm._call_gemini", new=AsyncMock()) as mock_llm:
            result = await classify_uncategorised(rows)
        mock_llm.assert_not_called()
        assert result == {}

    @pytest.mark.asyncio
    async def test_partial_batch_failure_marks_only_that_batch(self):
        """If first batch succeeds and a hypothetical second fails, only second marked failed."""
        good_response = json.dumps({
            "classifications": [{"index": 0, "category": "Transport"}]
        })
        with patch("app.services.llm._call_gemini", new=AsyncMock(return_value=good_response)):
            rows = [CleanedRow(
                txn_id="T", date="2024-01-01", merchant="Ola",
                amount=300.0, currency="INR", status="SUCCESS",
                category="Uncategorised", account_id="ACC001", notes=None, raw_index=0,
            )]
            result = await classify_uncategorised(rows)
        assert result[0].category == "Transport"
        assert not result[0].failed
