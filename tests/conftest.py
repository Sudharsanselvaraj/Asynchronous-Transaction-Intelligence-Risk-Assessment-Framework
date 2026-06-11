"""
tests/conftest.py

Shared fixtures and environment bootstrapping for all test modules.

IMPORTANT: os.environ.setdefault calls must happen at module level, before
any app code is imported, because get_settings() uses @lru_cache — whichever
call happens first wins and is cached for the entire test session.
"""
import csv
import os
import tempfile
from pathlib import Path

import pytest

# Set required env vars before any app import triggers get_settings()
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://test:test@localhost/testdb"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder-not-used-in-unit-tests")

from app.services.cleaning import CleanedRow  # noqa: E402 — must be after env setup


_CSV_FIELDS = [
    "txn_id", "date", "merchant", "amount", "currency",
    "status", "category", "account_id", "notes",
]


@pytest.fixture
def make_csv():
    """
    Factory fixture. Returns a callable that writes rows to a temp CSV file.
    All temp files are cleaned up after the test.

    Usage:
        def test_something(make_csv):
            path = make_csv([{"txn_id": "T1", "date": "2024-01-01", ...}])
            result = clean_csv(path)
    """
    created: list[Path] = []

    def _factory(rows: list[dict]) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, newline=""
        )
        writer = csv.DictWriter(tmp, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        tmp.close()
        path = Path(tmp.name)
        created.append(path)
        return path

    yield _factory

    for p in created:
        p.unlink(missing_ok=True)


@pytest.fixture
def base_csv_row() -> dict:
    """A clean, valid CSV row dict — override individual fields in tests."""
    return {
        "txn_id": "TXN001",
        "date": "04-09-2024",
        "merchant": "Swiggy",
        "amount": "500.00",
        "currency": "INR",
        "status": "SUCCESS",
        "category": "Food",
        "account_id": "ACC001",
        "notes": "",
    }


def make_cleaned_row(**overrides) -> CleanedRow:
    """Helper to build a CleanedRow with sensible defaults; override as needed."""
    defaults = dict(
        txn_id="T001",
        date="2024-01-01",
        merchant="Swiggy",
        amount=500.0,
        currency="INR",
        status="SUCCESS",
        category="Food",
        account_id="ACC001",
        notes=None,
        raw_index=0,
    )
    defaults.update(overrides)
    return CleanedRow(**defaults)
