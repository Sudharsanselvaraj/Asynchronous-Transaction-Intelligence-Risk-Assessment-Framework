# Asynchronous Transaction Intelligence & Risk Assessment Framework

> AI-powered CSV transaction analysis pipeline — async job queue architecture with statistical anomaly detection, LLM-driven categorisation, and structured risk reporting.

**Stack:** FastAPI · Celery · PostgreSQL · Redis · Google Gemini · Docker Compose · Alembic · Pydantic v2

---

## Overview

This system ingests CSV transaction files via a non-blocking REST API, processes them through a multi-stage worker pipeline, and produces structured risk assessments with narrative summaries. No synchronous blocking occurs in the API layer — all heavy computation is delegated to Celery workers.

**Processing pipeline (per job):**

```
Upload → SHA-256 dedup → Job record (pending) → Celery enqueue
         ↓
    [Worker: llm queue]
    ├── 1. clean_csv()               Normalise dates, strip currency symbols, dedup rows
    ├── 2. detect_anomalies()        Statistical outlier (3× median) + currency mismatch
    ├── 3. classify_uncategorised()  Batched Gemini calls, Pydantic-validated responses
    ├── 4. generate_narrative()      Single LLM call → risk level + spend narrative
    └── 5. bulk_insert()             Single DB commit for all transactions + summary
```

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Client                                                      │
└──────────────────┬───────────────────────────────────────────┘
                   │ POST /jobs/upload (multipart/form-data)
                   ▼
┌──────────────────────────────────────────────────────────────┐
│  FastAPI  :8000                                              │
│  ├── Stream file to /tmp/txn_uploads/{uuid}.csv              │
│  ├── SHA-256 deduplication check                             │
│  ├── Create Job(status=pending) → PostgreSQL                 │
│  └── Enqueue process_csv_task → Redis broker                 │
└──────────────────┬───────────────────────────────────────────┘
                   │ 201 {job_id} returned immediately
            (async)│
                   ▼
┌──────────────────────────────────────────────────────────────┐
│  Celery Worker  [queue: llm]                                 │
│  ├── clean_csv()          → normalise, dedup, ISO dates      │
│  ├── detect_anomalies()   → statistical + currency mismatch  │
│  ├── classify_*()         → batched Gemini (20 txn/call)     │
│  ├── generate_narrative() → risk level + 2-3 sentence report │
│  └── bulk_insert()        → single DB commit                 │
└──────────────────┬───────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────┐
│  PostgreSQL                                                  │
│  ├── jobs          (UUID PK, status, file_hash)              │
│  ├── transactions  (indexed by job_id, anomaly, category)    │
│  └── job_summaries (JSONB for top_merchants, breakdown)      │
└──────────────────────────────────────────────────────────────┘
```

**Network isolation:** DB and Redis are on an `internal` bridge network with no host exposure. Only the API container is on the `external` network.

---

## Quick Start

### Prerequisites

- Docker ≥ 24.0 and Docker Compose v2
- Google Gemini API key ([get one here](https://aistudio.google.com/))

### 1. Clone and configure

```bash
git clone https://github.com/your-org/txn-pipeline
cd txn-pipeline
cp .env.example .env
```

Edit `.env` — minimum required values:

```env
GEMINI_API_KEY=your-key-here
POSTGRES_PASSWORD=changeme
REDIS_PASSWORD=changeme
DATABASE_URL=postgresql+asyncpg://txnuser:changeme@db:5432/txndb
REDIS_URL=redis://:changeme@redis:6379/0
```

### 2. Start all services (migrations run automatically)

```bash
docker compose up --build
```

The `migrate` service runs `alembic upgrade head` before the API accepts traffic. All service dependencies are health-checked before dependents start.

### 3. Verify

```bash
curl http://localhost:8000/health
# → {"status": "ok"}

curl http://localhost:8000/ready
# → {"status": "ready"}
```

Swagger UI available at `http://localhost:8000/docs` (disabled in production).

---

## API Reference

### Upload CSV

```bash
POST /jobs/upload
Content-Type: multipart/form-data

curl -X POST http://localhost:8000/jobs/upload \
  -F "file=@transactions.csv"
```

**Response `201`:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "message": "Job queued for processing"
}
```

Constraints: max 10 MB, MIME types `text/csv` / `application/csv` / `text/plain`. Duplicate files (same SHA-256) are rejected.

---

### Poll job status

```bash
GET /jobs/{job_id}/status
```

**Response (in-progress):**
```json
{
  "job_id": "550e8400-...",
  "status": "processing",
  "filename": "transactions.csv",
  "row_count_raw": 500,
  "row_count_clean": 487,
  "created_at": "2025-01-01T10:00:00Z",
  "completed_at": null,
  "error_message": null,
  "summary": null
}
```

**Response (completed):**
```json
{
  "status": "completed",
  "summary": {
    "total_spend_inr": 45230.50,
    "total_spend_usd": 320.00,
    "top_merchants": ["Amazon", "Swiggy", "Zomato"],
    "anomaly_count": 3,
    "narrative": "Spend concentrated in Food and Shopping. Three high-value outliers detected in account ACC_7A2F.",
    "risk_level": "medium",
    "llm_failed": false
  }
}
```

Status values: `pending` → `processing` → `completed` | `failed`

---

### Get full results

```bash
GET /jobs/{job_id}/results
```

Returns `transactions[]`, `anomalies[]`, `category_breakdown[]`, and `summary`.

---

### List jobs

```bash
# All jobs (paginated)
GET /jobs?limit=20&offset=0

# Filter by status
GET /jobs?status=completed&limit=10
```

---

## CSV Format

Expected columns (case-insensitive, extra columns are ignored):

| Column | Type | Notes |
|---|---|---|
| `txn_id` | string | Optional, unique transaction identifier |
| `date` | string | Any common date format — normalised to ISO 8601 |
| `merchant` | string | Used for anomaly detection and LLM classification |
| `amount` | numeric | Currency symbols (`$`, `₹`, `,`) stripped automatically |
| `currency` | string | `INR` or `USD` |
| `status` | string | `SUCCESS`, `FAILED`, or `PENDING` |
| `category` | string | Rows with `Uncategorised` are sent to Gemini |
| `account_id` | string | Used for per-account statistical baseline |
| `notes` | string | Optional; `suspicious` keyword triggers soft anomaly signal |

---

## Anomaly Detection

Two detection strategies run in a single pass over cleaned rows:

**1. Statistical outlier**
Per `account_id`, compute the median amount across all `SUCCESS` transactions. Flag any transaction where `amount > median × 3.0`. The multiplier is configurable via `ANOMALY_MULTIPLIER` env var.

**2. Currency mismatch**
Flag `USD` transactions at merchants in the domestic-only list (`Swiggy`, `Ola`, `IRCTC`, `Zomato`, `Jio`). Configurable via `DOMESTIC_ONLY_MERCHANTS`.

Both conditions can fire simultaneously — reasons are concatenated with `;`.

---

## LLM Integration

**Model:** `gemini-1.5-flash` at `temperature=0.0` (deterministic)

**Classification:** Uncategorised rows are batched in groups of 20 and sent to Gemini with a structured prompt. Responses are validated against a Pydantic schema — invalid categories fall back to `"Other"` rather than failing.

**Narrative summary:** One call per job after all transactions are processed. Produces `risk_level` (`low`/`medium`/`high`) and a 2–3 sentence spending analysis.

**Resilience:**
- Exponential backoff: 3 retries with `delay = base_delay × 2ⁿ`
- JSON fence stripping for models that ignore `responseMimeType`
- `llm_failed=true` flag set on records if classification fails — job still completes
- PII pseudonymisation: `account_id` is SHA-256 hashed before any LLM call

**Redis-backed circuit breaker** (`app/utils/circuit_breaker.py`): opens after 5 consecutive failures, recovers after a configurable timeout. Returns `503` with fallback when open.

---

## Data Model

```
jobs
├── id               UUID PK
├── filename         string
├── original_filename string
├── file_hash        string(64)   SHA-256, indexed, used for dedup
├── status           enum         pending|processing|completed|failed
├── row_count_raw    int
├── row_count_clean  int
├── celery_task_id   string
├── error_message    text
└── created_at / updated_at / completed_at

transactions
├── id               UUID PK
├── job_id           UUID FK → jobs.id (CASCADE DELETE)
├── txn_id, date, merchant, amount, currency, status, category, account_id, notes
├── is_anomaly       bool
├── anomaly_reason   text
├── llm_category     string       set only when Gemini overrides source category
└── llm_failed       bool

job_summaries
├── id               UUID PK
├── job_id           UUID FK → jobs.id (unique, CASCADE DELETE)
├── total_spend_inr / total_spend_usd  float
├── top_merchants    JSONB        queryable, indexed
├── category_breakdown JSONB
├── anomaly_count    int
├── narrative        text
├── risk_level       string
└── llm_failed       bool
```

**Key design decisions:**
- UUID PKs — distributed-safe, no enumeration attacks
- `asyncpg` over `psycopg2` — native async driver, no thread-pool workarounds
- `NullPool` in Celery workers — fork-safe, avoids shared file descriptor corruption
- `JSONB` for `top_merchants` — queryable with `@>` and `?` operators, not raw TEXT
- Server-side `func.now()` for timestamps — DB clock authority, not application clock
- Composite indexes on `(job_id, is_anomaly)` and `(job_id, category)` for results queries

---

## Configuration Reference

All settings are loaded from environment variables via `pydantic-settings`. Never import at module level in tasks — use `get_settings()` to allow test overrides.

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `development` | `development` \| `testing` \| `production` |
| `DATABASE_URL` | required | `postgresql+asyncpg://user:pass@host/db` |
| `REDIS_URL` | required | `redis://:password@host:6379/0` |
| `GEMINI_API_KEY` | required | Gemini API key |
| `LLM_MODEL` | `gemini-1.5-flash` | Model name |
| `LLM_TEMPERATURE` | `0.0` | Set to 0 for deterministic outputs |
| `LLM_BATCH_SIZE` | `20` | Transactions per Gemini call |
| `LLM_RETRY_MAX` | `3` | Max retries on LLM failure |
| `LLM_RETRY_BASE_DELAY` | `1.0` | Base delay (seconds); doubles each attempt |
| `MAX_UPLOAD_BYTES` | `10485760` | 10 MB file size limit |
| `ANOMALY_MULTIPLIER` | `3.0` | Statistical outlier threshold multiplier |
| `CELERY_RESULT_EXPIRES` | `86400` | TTL (seconds) for Celery result tombstones |
| `DB_POOL_SIZE` | `10` | SQLAlchemy connection pool size |

---

## Running Tests

```bash
# Inside Docker (recommended — uses same DB/Redis)
docker compose run --rm api pytest tests/ -v

# Locally (requires .env with test DATABASE_URL)
pip install -e ".[dev]"
pytest tests/ -v
```

Coverage threshold: 70% (enforced via `--cov-fail-under=70`).

```bash
# Lint
ruff check app/

# Type check
mypy app/
```

---

## Scaling Analysis

| Bottleneck | Current limit | Failure mode |
|---|---|---|
| PostgreSQL connections | ~100 | 20 workers × pool_size=5 saturates at scale |
| Redis memory | 256 MB | Result tombstones accumulate without TTL |
| Worker concurrency | 4 per container | LLM calls are I/O-bound — CPU slots wasted |
| Upload disk | Single volume | Concurrent large uploads fill the shared volume |

**Enterprise path:**

- **DB:** PgBouncer in front of PostgreSQL (1000 app → 100 DB connections). Read replicas for all `GET /jobs/*` queries.
- **Workers:** `docker compose scale worker=10` with `--concurrency=20`. Separate `default` (status checks) and `llm` (processing) queues for priority.
- **Storage:** Replace `/tmp` volume with S3/MinIO — stream directly to Pandas without local disk.
- **LLM cache:** Redis cache keyed on `sha256(merchant_name)` — identical merchants classify identically, reducing LLM calls by ~40%.
- **API:** `slowapi` rate limiting per IP. CDN in front of GET endpoints.

---

## Project Structure

```
txn-pipeline/
├── app/
│   ├── api/
│   │   ├── deps.py                # FastAPI dependency injection
│   │   └── routers/jobs.py        # All /jobs/* endpoints
│   ├── core/
│   │   └── config.py              # pydantic-settings, all env vars
│   ├── db/
│   │   ├── session.py             # Async SQLAlchemy engine + NullPool worker engine
│   │   └── repository.py          # All DB queries — zero SQL in routes
│   ├── models/models.py           # SQLAlchemy ORM models
│   ├── schemas/schemas.py         # Pydantic v2 request/response schemas
│   ├── services/
│   │   ├── cleaning.py            # CSV normalisation — pure functions
│   │   ├── anomaly.py             # Statistical + currency mismatch detection
│   │   └── llm.py                 # Gemini integration, retry, PII pseudonymisation
│   ├── utils/circuit_breaker.py   # Redis-backed circuit breaker
│   ├── workers/
│   │   ├── celery_app.py          # Celery factory + queue routing
│   │   └── tasks.py               # Thin orchestrators — business logic in services/
│   └── main.py                    # FastAPI app factory, middleware, health probes
├── alembic/versions/001_initial.py
├── tests/unit/test_all.py
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

---

## Health & Observability

| Endpoint | Purpose | Use case |
|---|---|---|
| `GET /health` | Liveness probe — returns 200 if process is alive | Kubernetes `livenessProbe` |
| `GET /ready` | Readiness probe — verifies DB connectivity | Kubernetes `readinessProbe` |

All requests receive an `X-Request-ID` header (generated if not supplied). Log correlation is built in.

**Celery Beat cleanup task** runs every 2 hours to delete orphaned upload files from `/tmp/txn_uploads/` — prevents disk fill from failed jobs.

---

## License

MIT
