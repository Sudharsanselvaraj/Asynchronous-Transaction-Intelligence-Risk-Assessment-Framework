"""
tests/unit/test_cleaning.py
tests/unit/test_anomaly.py
tests/unit/test_llm.py
"""

# ═══════════════════════════════════════════════════════════════════════════════
# tests/unit/test_cleaning.py
# ═══════════════════════════════════════════════════════════════════════════════

import csv
import io
import tempfile
from pathlib import Path

import pytest

from app.services.cleaning import CleaningResult, clean_csv, _parse_date, _parse_amount


class TestParseDate:
    def test_dd_mm_yyyy(self):
        assert _parse_date("04-09-2024") == "2024-09-04"

    def test_yyyy_slash_mm_slash_dd(self):
        assert _parse_date("2024/02/05") == "2024-02-05"

    def test_already_iso(self):
        assert _parse_date("2024-01-15") == "2024-01-15"

    def test_invalid_returns_none(self):
        assert _parse_date("not-a-date") is None

    def test_empty_returns_none(self):
        assert _parse_date("") is None


class TestParseAmount:
    def test_plain_float(self):
        assert _parse_amount("10882.55") == 10882.55

    def test_dollar_prefix(self):
        assert _parse_amount("$11325.79") == 11325.79

    def test_commas(self):
        assert _parse_amount("1,234.56") == 1234.56

    def test_zero_returns_none(self):
        assert _parse_amount("0") is None

    def test_negative_returns_none(self):
        assert _parse_amount("-100") is None

    def test_empty_returns_none(self):
        assert _parse_amount("") is None


def _make_csv(rows: list[dict]) -> Path:
    """Write rows to a temp CSV file, return path."""
    fields = ["txn_id", "date", "merchant", "amount", "currency", "status", "category", "account_id", "notes"]
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    writer = csv.DictWriter(tmp, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    tmp.close()
    return Path(tmp.name)


class TestCleanCsv:
    BASE_ROW = {
        "txn_id": "TXN001",
        "date": "04-09-2024",
        "merchant": "Swiggy",
        "amount": "500.00",
        "currency": "INR",
        "status": "success",
        "category": "Food",
        "account_id": "ACC001",
        "notes": "",
    }

    def test_status_uppercased(self):
        path = _make_csv([self.BASE_ROW])
        result = clean_csv(path)
        assert result.rows[0].status == "SUCCESS"

    def test_currency_uppercased(self):
        row = {**self.BASE_ROW, "currency": "inr"}
        path = _make_csv([row])
        result = clean_csv(path)
        assert result.rows[0].currency == "INR"

    def test_dollar_sign_stripped(self):
        row = {**self.BASE_ROW, "amount": "$11325.79"}
        path = _make_csv([row])
        result = clean_csv(path)
        assert result.rows[0].amount == 11325.79

    def test_missing_category_becomes_uncategorised(self):
        row = {**self.BASE_ROW, "category": ""}
        path = _make_csv([row])
        result = clean_csv(path)
        assert result.rows[0].category == "Uncategorised"

    def test_blank_txn_id_gets_generated(self):
        row = {**self.BASE_ROW, "txn_id": ""}
        path = _make_csv([row])
        result = clean_csv(path)
        assert result.rows[0].txn_id.startswith("GEN-")

    def test_duplicate_rows_removed(self):
        path = _make_csv([self.BASE_ROW, self.BASE_ROW])  # exact duplicate
        result = clean_csv(path)
        assert result.raw_count == 2
        assert result.clean_count == 1
        assert result.duplicate_count == 1

    def test_missing_required_column_raises(self):
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
        tmp.write("merchant,amount\nSwiggy,100\n")
        tmp.close()
        with pytest.raises(ValueError, match="missing required columns"):
            clean_csv(Path(tmp.name))


# ═══════════════════════════════════════════════════════════════════════════════
# tests/unit/test_anomaly.py
# ═══════════════════════════════════════════════════════════════════════════════

from app.services.anomaly import detect_anomalies
from app.services.cleaning import CleanedRow


def _row(merchant, amount, currency="INR", account_id="ACC001", status="SUCCESS") -> CleanedRow:
    return CleanedRow(
        txn_id="T", date="2024-01-01", merchant=merchant,
        amount=amount, currency=currency, status=status,
        category="Food", account_id=account_id, notes=None, raw_index=0,
    )


class TestAnomalyDetection:
    def test_statistical_outlier_flagged(self):
        # Median of [100, 100, 100, 100] = 100. 400 > 3 * 100 → flag
        rows = [_row("M", 100)] * 4 + [_row("M", 400)]
        results = detect_anomalies(rows)
        assert results[-1].is_anomaly is True
        assert "median" in results[-1].reason

    def test_normal_transaction_not_flagged(self):
        rows = [_row("M", 100)] * 4 + [_row("M", 150)]
        results = detect_anomalies(rows)
        assert results[-1].is_anomaly is False

    def test_usd_at_domestic_merchant_flagged(self):
        rows = [_row("Swiggy", 500, currency="USD")]
        results = detect_anomalies(rows)
        assert results[0].is_anomaly is True
        assert "domestic" in results[0].reason.lower()

    def test_usd_at_foreign_merchant_not_flagged(self):
        rows = [_row("Amazon", 500, currency="USD")]
        results = detect_anomalies(rows)
        assert results[0].is_anomaly is False

    def test_returns_same_length_as_input(self):
        rows = [_row("M", i * 100) for i in range(1, 6)]
        results = detect_anomalies(rows)
        assert len(results) == len(rows)


# ═══════════════════════════════════════════════════════════════════════════════
# tests/unit/test_llm.py
# ═══════════════════════════════════════════════════════════════════════════════

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.llm import classify_uncategorised, generate_narrative_summary
from app.services.cleaning import CleanedRow


def _uncat_row(merchant: str) -> CleanedRow:
    return CleanedRow(
        txn_id="T", date="2024-01-01", merchant=merchant,
        amount=100.0, currency="INR", status="SUCCESS",
        category="Uncategorised", account_id="ACC001", notes=None, raw_index=0,
    )


class TestClassification:
    @pytest.mark.asyncio
    async def test_classifies_batch(self):
        mock_response = json.dumps({
            "classifications": [
                {"index": 0, "category": "Food"},
                {"index": 1, "category": "Transport"},
            ]
        })
        with patch("app.services.llm._call_gemini", new=AsyncMock(return_value=mock_response)):
            rows = [_uncat_row("Swiggy"), _uncat_row("Ola")]
            result = await classify_uncategorised(rows)
        assert result[0].category == "Food"
        assert result[1].category == "Transport"
        assert not result[0].failed

    @pytest.mark.asyncio
    async def test_invalid_category_becomes_other(self):
        mock_response = json.dumps({
            "classifications": [{"index": 0, "category": "Nonsense"}]
        })
        with patch("app.services.llm._call_gemini", new=AsyncMock(return_value=mock_response)):
            rows = [_uncat_row("Mystery")]
            result = await classify_uncategorised(rows)
        assert result[0].category == "Other"

    @pytest.mark.asyncio
    async def test_llm_failure_marks_failed_not_crash(self):
        with patch("app.services.llm._call_gemini", new=AsyncMock(side_effect=RuntimeError("API down"))):
            rows = [_uncat_row("Swiggy")]
            result = await classify_uncategorised(rows)
        assert result[0].failed is True
        assert result[0].category is None

    @pytest.mark.asyncio
    async def test_no_uncategorised_returns_empty(self):
        rows = [CleanedRow(
            txn_id="T", date="2024-01-01", merchant="Swiggy",
            amount=100.0, currency="INR", status="SUCCESS",
            category="Food",  # already categorised
            account_id="ACC001", notes=None, raw_index=0,
        )]
        result = await classify_uncategorised(rows)
        assert result == {}
