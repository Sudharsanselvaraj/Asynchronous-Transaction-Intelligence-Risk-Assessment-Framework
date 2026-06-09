"""
app/services/anomaly.py

Pure functions. Two detection strategies:
1. Statistical outlier — amount > 3x account median
2. Currency mismatch — USD on domestic-only Indian merchant
"""
import statistics
from dataclasses import dataclass

from app.services.cleaning import CleanedRow


DOMESTIC_ONLY_MERCHANTS = {"swiggy", "ola", "irctc", "zomato", "jio recharge", "jio"}


@dataclass
class AnomalyResult:
    is_anomaly: bool
    reason: str | None


def detect_anomalies(rows: list[CleanedRow], multiplier: float = 3.0) -> list[AnomalyResult]:
    """
    Returns one AnomalyResult per input row, in the same order.

    Strategy 1 — Statistical outlier:
      Per account_id, compute median of amounts (SUCCESS only).
      Flag any transaction where amount > median * multiplier.

    Strategy 2 — Currency mismatch:
      Flag USD transactions at domestic Indian merchants.
    """
    # Build per-account medians using only SUCCESS transactions with valid amounts
    account_amounts: dict[str, list[float]] = {}
    for row in rows:
        if (
            row.account_id
            and row.amount is not None
            and row.status == "SUCCESS"
        ):
            account_amounts.setdefault(row.account_id, []).append(row.amount)

    account_medians: dict[str, float] = {}
    for acc_id, amounts in account_amounts.items():
        if amounts:
            account_medians[acc_id] = statistics.median(amounts)

    results: list[AnomalyResult] = []

    for row in rows:
        reasons: list[str] = []

        # ── Statistical outlier ──────────────────────────────────────
        if (
            row.account_id
            and row.amount is not None
            and row.account_id in account_medians
        ):
            median = account_medians[row.account_id]
            if median > 0 and row.amount > median * multiplier:
                reasons.append(
                    f"Amount {row.amount:.2f} exceeds {multiplier}x account median "
                    f"({median:.2f}) for {row.account_id}"
                )

        # ── Currency mismatch ────────────────────────────────────────
        if (
            row.currency == "USD"
            and row.merchant
            and row.merchant.lower() in DOMESTIC_ONLY_MERCHANTS
        ):
            reasons.append(
                f"USD transaction at domestic-only merchant '{row.merchant}'"
            )

        # ── Notes-based hint (soft signal, not primary detector) ─────
        if row.notes and "suspicious" in row.notes.lower():
            reasons.append("Marked SUSPICIOUS in source data notes")

        if reasons:
            results.append(AnomalyResult(is_anomaly=True, reason="; ".join(reasons)))
        else:
            results.append(AnomalyResult(is_anomaly=False, reason=None))

    return results
