# Transaction Processing Pipeline

[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg?logo=fastapi)](https://fastapi.tiangolo.com/)
[![Celery](https://img.shields.io/badge/Celery-5.4-37814A.svg)](https://docs.celeryq.dev/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791.svg?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-7-DC382D.svg?logo=redis&logoColor=white)](https://redis.io/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED.svg?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![Tests](https://img.shields.io/badge/tests-51%20passing-brightgreen.svg)](#development)
[![Coverage](https://img.shields.io/badge/coverage-90%25-brightgreen.svg)](#development)

Async AI-powered financial transaction analysis API. Upload a dirty CSV, get back cleaned data, flagged anomalies, LLM-classified categories, and a narrative risk summary — all processed in the background while the API returns immediately.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [API Reference](#api-reference)
- [Processing Pipeline](#processing-pipeline)
- [Environment Variables](#environment-variables)
- [Design Decisions](#design-decisions)
- [Scalability Analysis](#scalability-analysis)
- [Development](#development)

---

## Overview

The service accepts a raw transaction CSV (dirty data: mixed date formats, inconsistent casing, missing fields, duplicates), processes it through a four-stage async pipeline, and exposes results via a polling REST API.

**Key properties:**

- **Non-blocking uploads** — file is streamed to disk; job ID returned in under 100ms; all processing happens in a Celery worker
- **Idempotent uploads** — SHA-256 content hash deduplication; re-uploading the same file returns the existing job instantly
- **Graceful LLM failure** — if Gemini is unavailable after 3 retries, affected rows are marked `llm_failed=true`; the job still completes with all other data
- **Zero manual setup** — Alembic migrations run automatically in an init container; `docker compose up --build` is the only command needed
- **Structured logging** — every log line is a JSON object with timestamp, logger, and structured context fields; compatible with Datadog, CloudWatch, and Loki

**What the pipeline does with `transactions.csv` (included):**

| Stage | Input | Output |
|---|---|---|
| Cleaning | 95 raw rows | 85 rows (10 exact duplicates removed) |
| Anomaly detection | 85 rows | 22 flagged (statistical outliers + USD at domestic merchants + high-value failed) |
| LLM classification | 13 uncategorised rows | Categories assigned via Gemini |
| Narrative summary | Aggregated stats | Risk level + 2-3 sentence narrative |

---

## Architecture

```
Client (curl / Postman / frontend)
        |
        | HTTP
        v
+-----------------------------------------------+
|  FastAPI  (port 8000)                         |
|                                               |
|  POST /jobs/upload                            |
|    1. Validate MIME type, extension, size     |
|    2. Stream file to disk (64KB chunks)       |
|    3. SHA-256 hash for deduplication          |
|    4. INSERT Job(status=pending)              |
|    5. ENQUEUE task → Redis                    |
|    6. Return job_id in <100ms                 |
|                                               |
|  GET /jobs/{id}/status   → SELECT Job+Summary |
|  GET /jobs/{id}/results  → SELECT Job+Txns   |
|  GET /jobs               → SELECT Jobs        |
+-------+------+------+------------------------+
        |      |      |
        | DB   | DB   | Enqueue
        v      v      v
  +----------+    +----------+
  |PostgreSQL|    |  Redis   |
  |  jobs    |    | db0:     |
  | transactions|  |  broker  |
  |job_summaries| | db1:    |
  +----------+    |  results |
                  +----+-----+
                       |
                       | Dequeue
                       v
        +-------------------------------+
        |  Celery Worker                |
        |                               |
        |  1. clean_csv()               |
        |     Normalise dates, strip $  |
        |     Uppercase status/currency |
        |     Remove duplicates         |
        |                               |
        |  2. detect_anomalies()        |
        |     Statistical outlier       |
        |     Currency mismatch         |
        |     High-value failed         |
        |                               |
        |  3. classify_uncategorised()  |
        |     Batched Gemini calls      |
        |     (max 20 rows per call)    |
        |                               |
        |  4. generate_narrative()      |
        |     Single Gemini call        |
        |                               |
        |  5. bulk_insert()             |
        |     One DB commit for all     |
        +---------------+---------------+
                        |
                        | HTTPS (retried 3x, exp backoff)
                        v
                +------------------+
                | Gemini 1.5 Flash |
                | External API     |
                +------------------+
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for Mermaid sequence, ER, and component diagrams.

---

## Quick Start

**Prerequisites:** Docker and Docker Compose.

```bash
# 1. Clone the repository
git clone <repo-url>
cd txn-pipeline

# 2. Configure environment
cp .env.example .env
# Edit .env:
#   GEMINI_API_KEY=your_key   (free at https://aistudio.google.com/)
#   POSTGRES_PASSWORD=...     (any strong string)
#   REDIS_PASSWORD=...        (any strong string)

# 3. Start all services — migrations run automatically
docker compose up --build

# 4. Verify the API is up
curl http://localhost:8000/health
# {"status":"ok"}

curl http://localhost:8000/ready
# {"status":"ready","database":"up"}
```

> The `migrate` service runs `alembic upgrade head` and exits before the API starts accepting traffic. The API depends on `migrate` completing successfully.

### Upload and process the included CSV

```bash
# Upload
curl -X POST http://localhost:8000/jobs/upload \
  -F "file=@transactions.csv"
# {"job_id":"550e8400-...","status":"pending","message":"..."}

# Poll status
curl http://localhost:8000/jobs/550e8400-.../status

# Get full results
curl http://localhost:8000/jobs/550e8400-.../results
```

---

## API Reference

### POST /jobs/upload

Accept a CSV file. Stream it to disk, validate headers, deduplicate by SHA-256 hash, create a job record, and enqueue processing. Returns the job ID immediately.

**Request**
```bash
curl -X POST http://localhost:8000/jobs/upload \
  -F "file=@transactions.csv"
```

**Response 201** — new job created
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "message": "Job enqueued. Poll /jobs/{job_id}/status for updates."
}
```

**Response 200** — duplicate file detected, returns existing job
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "message": "Duplicate file — returning existing job"
}
```

**Error codes**
| HTTP | Code | Trigger |
|------|------|---------|
| 413 | `FILE_TOO_LARGE` | File exceeds 10MB |
| 422 | `INVALID_MIME_TYPE` | Not a CSV MIME type |
| 422 | `INVALID_EXTENSION` | Filename does not end in `.csv` |

---

### GET /jobs/{job_id}/status

Poll job progress. When status is `completed`, includes the full summary object.

**Request**
```bash
curl http://localhost:8000/jobs/550e8400-e29b-41d4-a716-446655440000/status
```

**Response — processing**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "processing",
  "filename": "transactions.csv",
  "row_count_raw": null,
  "row_count_clean": null,
  "created_at": "2024-09-04T10:00:00Z",
  "completed_at": null,
  "error_message": null,
  "summary": null
}
```

**Response — completed**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "filename": "transactions.csv",
  "row_count_raw": 95,
  "row_count_clean": 85,
  "created_at": "2024-09-04T10:00:00Z",
  "completed_at": "2024-09-04T10:00:45Z",
  "summary": {
    "total_spend_inr": 642381.50,
    "total_spend_usd": 58430.20,
    "top_merchants": ["Flipkart", "IRCTC", "Ola"],
    "anomaly_count": 22,
    "narrative": "Spending is concentrated in e-commerce and travel...",
    "risk_level": "high",
    "llm_failed": false
  }
}
```

---

### GET /jobs/{job_id}/results

Full output for a completed job: cleaned transactions, anomalies, category breakdown, and narrative summary. Returns `409` if the job is not yet completed.

**Request**
```bash
curl http://localhost:8000/jobs/550e8400-e29b-41d4-a716-446655440000/results
```

**Response 200**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "transactions": [
    {
      "id": "...",
      "txn_id": "TXN1065",
      "date": "2024-09-04",
      "merchant": "Flipkart",
      "amount": 10882.55,
      "currency": "INR",
      "status": "SUCCESS",
      "category": "Shopping",
      "account_id": "ACC003",
      "is_anomaly": false,
      "anomaly_reason": null,
      "llm_category": null,
      "llm_failed": false
    }
  ],
  "anomalies": [
    {
      "txn_id": "TXN2003",
      "merchant": "IRCTC",
      "amount": 193647.29,
      "currency": "INR",
      "reason": "Amount 193647.29 exceeds 3.0x account median (9582.58) for ACC002"
    }
  ],
  "category_breakdown": [
    {"category": "Shopping", "total_spend": 215340.10, "transaction_count": 18},
    {"category": "Travel", "total_spend": 180920.50, "transaction_count": 12}
  ],
  "summary": { ... }
}
```

**Response 409** — job not yet completed
```json
{
  "detail": {
    "code": "JOB_NOT_COMPLETED",
    "message": "Job is processing, results not yet available"
  }
}
```

---

### GET /jobs

List all jobs with optional status filter and cursor pagination.

**Request**
```bash
# All jobs
curl "http://localhost:8000/jobs"

# Filter by status
curl "http://localhost:8000/jobs?status=completed&limit=10&offset=0"

# Available status values: pending, processing, completed, failed
```

**Response 200**
```json
{
  "items": [
    {
      "job_id": "550e8400-...",
      "status": "completed",
      "filename": "transactions.csv",
      "row_count_raw": 95,
      "created_at": "2024-09-04T10:00:00Z"
    }
  ],
  "total": 1,
  "limit": 20,
  "offset": 0
}
```

---

### GET /health

Kubernetes liveness probe. Returns `200` if the process is alive.

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### GET /ready

Kubernetes readiness probe. Returns `200` only if the database is reachable. The load balancer stops sending traffic if this returns `503`.

```bash
curl http://localhost:8000/ready
# 200 → {"status":"ready","database":"up"}
# 503 → {"status":"not ready","database":"down","reason":"..."}
```

### GET /dashboard/health

Detailed health dashboard for internal monitoring: database connection pool, Redis client count, and active Celery tasks.

```bash
curl http://localhost:8000/dashboard/health
```
```json
{
  "timestamp": "2024-09-04T10:00:00.123456",
  "database": {"status": "ok", "pool_size": 10, "checked_out": 2},
  "redis": {"status": "ok", "connected_clients": 4},
  "celery": {"status": "ok", "active_tasks": 1}
}
```

---

## Processing Pipeline

### Stage 1: Data Cleaning (`app/services/cleaning.py`)

Pure function — no I/O side effects; fully unit-testable.

| Transformation | Logic |
|---|---|
| Date normalisation | Accepts `DD-MM-YYYY`, `YYYY/MM/DD`, `YYYY-MM-DD`; outputs ISO 8601 |
| Amount cleaning | Strips `$`, commas, and spaces; rejects negatives and zeroes |
| Status/Currency | Uppercased; invalid values stored as `null` |
| Missing category | Filled with `"Uncategorised"` |
| Missing `txn_id` | Generated as `GEN-{8-char hex}` |
| Deduplication | Signature: `(txn_id, date, merchant, amount)`; first occurrence kept |

### Stage 2: Anomaly Detection (`app/services/anomaly.py`)

Four independent rules applied per row; multiple can fire simultaneously.

| Rule | Trigger | Example |
|---|---|---|
| Statistical outlier | `amount > 3× account median` (SUCCESS rows only) | TXN2003: ₹193,647 vs median ₹9,582 |
| Currency mismatch | USD transaction at domestic-only Indian merchant | Zomato charged in USD |
| High-value failed | `amount > ₹5,000 AND status = FAILED` | ₹9,092 FAILED Flipkart |
| Source annotation | Notes field contains "suspicious" (case-insensitive) | Notes: "SUSPICIOUS" |

### Stage 3: LLM Classification (`app/services/llm.py`)

Only rows with `category = "Uncategorised"` are sent to Gemini.

- Batched: max 20 rows per API call (configurable via `LLM_BATCH_SIZE`)
- PII-safe: only `merchant`, `amount`, and `currency` are sent — no `account_id`
- Response validated by Pydantic schema before persistence; invalid categories coerced to `"Other"`
- On failure: rows marked `llm_failed=true`; job continues

### Stage 4: Narrative Summary

Single Gemini call with pre-computed aggregates (spend by currency, top merchants, anomaly count). Response includes a 2-3 sentence narrative and a `risk_level` of `low/medium/high`. Returns `null` gracefully if the LLM fails.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | Yes | — | `postgresql+asyncpg://user:pass@host/db` |
| `REDIS_URL` | Yes | — | `redis://:password@host:6379` |
| `GEMINI_API_KEY` | Yes | — | Free key at [aistudio.google.com](https://aistudio.google.com/) |
| `POSTGRES_PASSWORD` | Yes | — | Docker Compose PostgreSQL password |
| `REDIS_PASSWORD` | Yes | — | Redis auth password |
| `POSTGRES_USER` | No | `txnuser` | PostgreSQL username |
| `POSTGRES_DB` | No | `txndb` | PostgreSQL database name |
| `APP_ENV` | No | `development` | `development` / `testing` / `production` |
| `LOG_LEVEL` | No | `INFO` | Python log level |
| `UPLOAD_DIR` | No | `/tmp/txn_uploads` | Uploaded CSV storage path |
| `MAX_UPLOAD_BYTES` | No | `10485760` | 10MB file size limit |
| `LLM_MODEL` | No | `gemini-1.5-flash` | Gemini model name |
| `LLM_BATCH_SIZE` | No | `20` | Max transactions per LLM call |
| `LLM_RETRY_MAX` | No | `3` | LLM retry attempts |
| `LLM_RETRY_BASE_DELAY` | No | `1.0` | Exponential backoff base (seconds) |
| `ANOMALY_MULTIPLIER` | No | `3.0` | Statistical outlier threshold multiplier |
| `DB_POOL_SIZE` | No | `10` | SQLAlchemy connection pool size |

---

## Design Decisions

Full reasoning with tradeoffs in [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md).

| Decision | Choice | Primary Reason |
|---|---|---|
| API framework | FastAPI | Async-native; Pydantic v2 validation; auto OpenAPI docs |
| Database | PostgreSQL | JSONB for summaries; ACID for job state transitions |
| Job queue | Celery + Redis | Late ack; reject-on-worker-lost; beat scheduler |
| LLM | Gemini 1.5 Flash | Free tier; `responseMimeType: "application/json"` |
| ORM | SQLAlchemy async | Non-blocking; type-safe mapped columns |
| Worker engine | NullPool | Fork-safe: no shared file descriptors across `fork()` |
| File upload | Stream to disk | Never loads entire file into RAM; supports large CSVs |
| Deduplication | SHA-256 hash | Prevents re-processing identical files; O(1) check |

---

## Scalability Analysis

### Where the system breaks at 100x load

| Bottleneck | Current Limit | Failure Mode | Mitigation |
|---|---|---|---|
| PostgreSQL connections | ~100 (20 workers × pool_size=5) | `too many connections` | Add PgBouncer in front |
| Redis memory | 256MB hard cap | OOM eviction of result tombstones | Increase limit; separate instance for results |
| Gemini rate limits | Free tier: 15 req/min | `429 Too Many Requests` | Redis-based rate limiter; move to paid tier |
| Upload disk | Single Docker volume | Full disk with concurrent large uploads | Replace with S3/MinIO; stream directly |
| Worker concurrency | 4 per container | Task backlog accumulates | `docker compose scale worker=10` |

### Enterprise re-architecture

1. **PgBouncer** — Pool 1,000 app connections → 100 DB connections; read replicas for all `GET` queries
2. **S3 / MinIO** — Eliminate shared volume; workers read directly from object storage
3. **Redis LLM cache** — Cache classification results keyed on `sha256(merchant)` for ~40% token cost reduction
4. **Horizontal worker scaling** — Separate `llm` queue (slow) from `default` queue (fast); scale independently
5. **Rate limiting** — Token bucket per API key in Redis; 429 with `Retry-After` header

---

## Development

### Run tests
```bash
# Local (requires .env with valid or test values)
python -m pytest tests/ -v

# In Docker — no local Python needed
docker compose run --rm api pytest tests/ -v --cov=app/services

# Current: 51 tests, 90% service layer coverage
```

### Run linting
```bash
.venv/bin/python -m ruff check app/ tests/
```

### Apply migrations manually
```bash
# In Docker
docker compose run --rm migrate

# Locally (requires DATABASE_URL set)
alembic upgrade head
```

### Generate a new migration
```bash
alembic revision --autogenerate -m "add_index_on_merchant"
```

### View structured logs
```bash
# Pretty-print JSON logs with jq
docker compose logs -f api | grep -v "^$" | jq '.'

# Filter by level
docker compose logs -f worker | jq 'select(.level == "ERROR")'

# Filter by job_id
docker compose logs -f worker | jq 'select(.job_id == "550e8400-...")'
```

### Project layout
```
app/
  api/
    routers/jobs.py       # All /jobs/* endpoints
    deps.py               # FastAPI dependency injection
  core/
    config.py             # Pydantic settings (env vars)
    logging_config.py     # Structured JSON logging setup
  db/
    session.py            # Engine factory (API pool + worker NullPool)
    repository.py         # All SQL queries
  models/models.py        # SQLAlchemy ORM models
  schemas/schemas.py      # Pydantic request/response schemas
  services/
    cleaning.py           # Pure: CSV normalisation
    anomaly.py            # Pure: anomaly detection rules
    llm.py                # Gemini API calls with retry + Pydantic validation
  workers/
    celery_app.py         # Celery configuration
    tasks.py              # Pipeline orchestration task
  dashboard/health.py     # /dashboard/health endpoint
  main.py                 # FastAPI app factory
tests/
  conftest.py             # Shared fixtures + env bootstrapping
  unit/
    test_all.py           # Core service tests (27 tests)
    test_anomaly_extra.py # Extended anomaly rule tests (16 tests)
    test_llm_extra.py     # Extended LLM service tests (8 tests)
```
