"""
app/services/cleaning.py

Pure functions — no DB, no I/O, fully unit-testable.
Returns structured dataclasses, not raw dicts.
"""
import csv
import hashlib
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


VALID_DATE_FORMATS = [
    "%d-%m-%Y",   # 04-09-2024
    "%Y/%m/%d",   # 2024/02/05
    "%Y-%m-%d",   # already ISO
    "%d/%m/%Y",   # 04/09/2024
]

VALID_STATUSES = {"SUCCESS", "FAILED", "PENDING"}


@dataclass
class CleanedRow:
    txn_id: str
    date: str | None
    merchant: str | None
    amount: float | None
    currency: str | None
    status: str | None
    category: str
    account_id: str | None
    notes: str | None
    raw_index: int


@dataclass
class CleaningResult:
    rows: list[CleanedRow] = field(default_factory=list)
    raw_count: int = 0
    clean_count: int = 0
    duplicate_count: int = 0
    skipped_rows: list[dict] = field(default_factory=list)


def sha256_file(path: Path) -> str:
    """SHA-256 of file content for deduplication."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_date(raw: str) -> str | None:
    """Try all known formats, return ISO 8601 string or None."""
    raw = raw.strip()
    for fmt in VALID_DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_amount(raw: str) -> float | None:
    """Strip currency symbols, commas, spaces; return float or None."""
    cleaned = re.sub(r"[^\d.]", "", raw.strip())
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None


def _normalise_currency(raw: str) -> str | None:
    upper = raw.strip().upper()
    if upper in ("INR", "USD"):
        return upper
    return None


def clean_csv(path: Path) -> CleaningResult:
    """
    Read CSV from disk path (never loads whole file in memory).
    Returns CleaningResult with typed, deduplicated rows.
    """
    result = CleaningResult()
    seen_signatures: set[tuple] = set()

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        # Validate expected columns
        required = {"txn_id", "date", "merchant", "amount", "currency", "status", "account_id"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(
                f"CSV missing required columns. Found: {reader.fieldnames}"
            )

        for i, raw_row in enumerate(reader):
            result.raw_count += 1

            # ── Generate txn_id if missing ──────────────────────────
            txn_id = raw_row.get("txn_id", "").strip() or f"GEN-{uuid.uuid4().hex[:8].upper()}"

            # ── Date ────────────────────────────────────────────────
            date_str = _parse_date(raw_row.get("date", ""))

            # ── Amount ──────────────────────────────────────────────
            amount = _parse_amount(raw_row.get("amount", ""))

            # ── Currency ────────────────────────────────────────────
            currency = _normalise_currency(raw_row.get("currency", ""))

            # ── Status ──────────────────────────────────────────────
            status_raw = raw_row.get("status", "").strip().upper()
            status = status_raw if status_raw in VALID_STATUSES else None

            # ── Category ────────────────────────────────────────────
            category = raw_row.get("category", "").strip() or "Uncategorised"

            # ── Merchant ────────────────────────────────────────────
            merchant = raw_row.get("merchant", "").strip() or None

            # ── Deduplication signature ─────────────────────────────
            # Two rows are duplicates if they match on these 4 fields
            sig = (txn_id, date_str, merchant, amount)
            if sig in seen_signatures:
                result.duplicate_count += 1
                continue
            seen_signatures.add(sig)

            result.rows.append(
                CleanedRow(
                    txn_id=txn_id,
                    date=date_str,
                    merchant=merchant,
                    amount=amount,
                    currency=currency,
                    status=status,
                    category=category,
                    account_id=raw_row.get("account_id", "").strip() or None,
                    notes=raw_row.get("notes", "").strip() or None,
                    raw_index=i,
                )
            )

    result.clean_count = len(result.rows)
    return result
