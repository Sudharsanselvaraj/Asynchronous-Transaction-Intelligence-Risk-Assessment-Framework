"""
tests/unit/test_anomaly_extra.py

Extended anomaly detection tests covering:
- High-value FAILED transaction rule
- None amount / None account_id edge cases
- Multi-reason combinations
- Boundary values
"""
import pytest

from app.services.anomaly import HIGH_VALUE_FAILED_THRESHOLD, detect_anomalies
from app.services.cleaning import CleanedRow


def _row(
    merchant: str = "Flipkart",
    amount: float = 500.0,
    status: str = "SUCCESS",
    currency: str = "INR",
    account_id: str | None = "ACC001",
    notes: str | None = None,
) -> CleanedRow:
    return CleanedRow(
        txn_id="T",
        date="2024-01-01",
        merchant=merchant,
        amount=amount,
        currency=currency,
        status=status,
        category="Food",
        account_id=account_id,
        notes=notes,
        raw_index=0,
    )


class TestHighValueFailedRule:
    def test_failed_above_threshold_flagged(self):
        rows = [_row(amount=HIGH_VALUE_FAILED_THRESHOLD + 1, status="FAILED")]
        results = detect_anomalies(rows)
        assert results[0].is_anomaly is True
        assert "FAILED" in results[0].reason

    def test_failed_at_exact_threshold_not_flagged(self):
        # Boundary: threshold itself is NOT above threshold (strict >)
        rows = [_row(amount=HIGH_VALUE_FAILED_THRESHOLD, status="FAILED")]
        results = detect_anomalies(rows)
        # Should not trigger the high-value-failed rule
        assert "FAILED" not in (results[0].reason or "")

    def test_failed_below_threshold_not_flagged(self):
        rows = [_row(amount=100.0, status="FAILED")]
        results = detect_anomalies(rows)
        assert results[0].is_anomaly is False

    def test_success_above_threshold_not_flagged_by_failed_rule(self):
        # HIGH SUCCESS does not trigger the FAILED rule
        # (may trigger statistical outlier if there are other rows for context)
        rows = [_row(amount=HIGH_VALUE_FAILED_THRESHOLD + 1, status="SUCCESS")]
        results = detect_anomalies(rows)
        assert "FAILED" not in (results[0].reason or "")

    def test_pending_above_threshold_not_flagged(self):
        rows = [_row(amount=HIGH_VALUE_FAILED_THRESHOLD + 1, status="PENDING")]
        results = detect_anomalies(rows)
        assert "FAILED" not in (results[0].reason or "")

    def test_domestic_usd_and_high_value_failed_combine(self):
        # Swiggy + USD + FAILED + high amount → all three anomaly signals
        rows = [_row("Swiggy", HIGH_VALUE_FAILED_THRESHOLD + 1, status="FAILED", currency="USD")]
        results = detect_anomalies(rows)
        assert results[0].is_anomaly is True
        reason = results[0].reason
        assert "FAILED" in reason
        assert "domestic" in reason.lower()

    def test_reason_includes_amount_value(self):
        rows = [_row(amount=12345.67, status="FAILED")]
        results = detect_anomalies(rows)
        assert "12,345.67" in results[0].reason


class TestNullEdgeCases:
    def test_none_amount_skips_all_numeric_checks(self):
        """Rows cleaned to None amount must not raise anywhere."""
        row = CleanedRow(
            txn_id="T", date="2024-01-01", merchant="Swiggy",
            amount=None, currency="INR", status="FAILED",
            category="Food", account_id="ACC001", notes=None, raw_index=0,
        )
        results = detect_anomalies([row])
        # FAILED rule requires amount is not None
        assert "FAILED" not in (results[0].reason or "")

    def test_none_account_id_skips_statistical_check(self):
        """No account_id means no median context — high amount must not raise."""
        row = CleanedRow(
            txn_id="T", date="2024-01-01", merchant="Amazon",
            amount=999_999.0, currency="INR", status="SUCCESS",
            category="Shopping", account_id=None, notes=None, raw_index=0,
        )
        results = detect_anomalies([row])
        assert "median" not in (results[0].reason or "")

    def test_all_failed_rows_median_computed_from_none(self):
        """If all rows are FAILED, median dict is empty; no statistical flag."""
        rows = [
            _row(amount=100.0, status="FAILED"),
            _row(amount=200.0, status="FAILED"),
            _row(amount=900.0, status="FAILED"),
        ]
        results = detect_anomalies(rows)
        # No SUCCESS rows → no median → no statistical outlier flag
        for r in results:
            assert "median" not in (r.reason or "")

    def test_empty_input_returns_empty(self):
        assert detect_anomalies([]) == []

    def test_output_length_matches_input_with_mixed_rows(self):
        rows = [
            _row(merchant="Swiggy", amount=100.0),
            _row(merchant="Zomato", amount=200.0, currency="USD"),
            _row(merchant="Amazon", amount=HIGH_VALUE_FAILED_THRESHOLD + 1, status="FAILED"),
        ]
        results = detect_anomalies(rows)
        assert len(results) == 3


class TestSuspiciousNotesRule:
    def test_suspicious_note_case_insensitive(self):
        rows = [_row(notes="SUSPICIOUS transaction")]
        results = detect_anomalies(rows)
        assert results[0].is_anomaly is True

    def test_lowercase_suspicious_flagged(self):
        rows = [_row(notes="this looks suspicious to me")]
        results = detect_anomalies(rows)
        assert results[0].is_anomaly is True

    def test_non_suspicious_note_not_flagged(self):
        rows = [_row(notes="Refund expected")]
        results = detect_anomalies(rows)
        assert results[0].is_anomaly is False

    def test_none_notes_not_flagged(self):
        rows = [_row(notes=None)]
        results = detect_anomalies(rows)
        assert results[0].is_anomaly is False
