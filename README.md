# Transaction Processing Pipeline

AI-powered CSV transaction analysis. FastAPI + Celery + PostgreSQL + Redis + Gemini.

## Quick Start

```bash
# 1. Clone and configure
git clone https://github.com/your-org/txn-pipeline
cd txn-pipeline
cp .env.example .env
# Edit .env — set GEMINI_API_KEY, POSTGRES_PASSWORD, REDIS_PASSWORD

# 2. Start everything (runs migrations automatically)
docker compose up --build

# 3. Verify
curl http://localhost:8000/health
# → {"status": "ok"}
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Client                                                         │
└───────────────────┬─────────────────────────────────────────────┘
                    │ POST /jobs/upload (multipart/form-data)
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│  FastAPI (api)           port 8000                              │
│  ├── Stream file to disk (/tmp/txn_uploads/{uuid}.csv)          │
│  ├── SHA-256 deduplication check                                │
│  ├── Create Job(status=pending) in PostgreSQL                   │
│  └── Enqueue process_csv_task → Redis broker                    │
└───────────────────┬─────────────────────────────────────────────┘
                    │ Returns job_id immediately (201)
                    │
              (async)│
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│  Celery Worker (worker)   queue: llm                            │
│  ├── a) clean_csv()        → normalise dates, strip $, dedup    │
│  ├── b) detect_anomalies() → statistical + currency mismatch    │
│  ├── c) classify_uncategorised() → batched LLM calls            │
│  ├── d) generate_narrative_summary() → single LLM call          │
│  └── bulk_insert() → single DB commit for all transactions      │
└───────────────────┬─────────────────────────────────────────────┘
                    │ Writes to PostgreSQL
                    ▼
┌────────────────────────────────────────┐
│  PostgreSQL                            │
│  ├── jobs (UUID PK, status, file_hash) │
│  ├── transactions (indexed by job_id)  │
│  └── job_summaries (JSONB columns)     │
└────────────────────────────────────────┘
```

**Request lifecycle:** Client → FastAPI routes → Repository (async SQLAlchemy) → PostgreSQL. Processing is never synchronous in the API — the worker does all heavy lifting.

## API Reference

### Upload CSV
```bash
curl -X POST http://localhost:8000/jobs/upload \
  -F "file=@transactions.csv"
# → {"job_id": "550e8400-...", "status": "pending", "message": "..."}
```

### Poll status
```bash
curl http://localhost:8000/jobs/550e8400-.../status
# → {"job_id": "...", "status": "processing", ...}
# → {"job_id": "...", "status": "completed", "summary": {...}}
```

### Get results
```bash
curl http://localhost:8000/jobs/550e8400-.../results
# → {"transactions": [...], "anomalies": [...], "category_breakdown": [...], "summary": {...}}
```

### List jobs
```bash
# All jobs
curl http://localhost:8000/jobs

# Filter by status
curl "http://localhost:8000/jobs?status=completed&limit=10&offset=0"
```

## Scalability Analysis

### Where it breaks at 100× load

| Bottleneck | Current limit | Why it breaks |
|---|---|---|
| PostgreSQL connections | ~100 connections | 20 workers × 5 pool = 100 connections hit |
| Redis memory | 256MB limit | Result tombstones accumulate if TTL not set |
| Worker concurrency | 4 per container | LLM calls are I/O-bound, not CPU-bound |
| Upload disk | Single volume | Concurrent 10MB uploads fill disk |

### Enterprise re-architecture

1. **Database:** PgBouncer connection pooler in front of PostgreSQL. Pool 1000 app connections → 100 DB connections. Use read replicas for `GET /jobs/*` queries.

2. **Workers:** Scale horizontally with `docker compose scale worker=10`. Use `--concurrency=20` (I/O-bound LLM tasks don't need CPU per slot). Add priority queue: cheap `default` queue for status checks, `llm` queue for processing.

3. **Storage:** Replace local `/tmp` volume with S3 (or MinIO in self-hosted). Stream from S3 key directly to Pandas without local disk.

4. **LLM:** Add response caching (Redis) keyed on merchant name hash — same merchant classifies the same way. Reduces LLM calls by ~40%.

5. **API:** Add rate limiting per IP (slowapi or nginx `limit_req`). Add CDN in front for GET endpoints.

## Running Tests

```bash
docker compose run --rm api pytest tests/ -v
```

## Design Decisions

**Why FastAPI over Django REST Framework?** Async-native. SQLAlchemy async + asyncpg gives true non-blocking DB access. DRF requires workarounds for async.

**Why asyncpg over psycopg2?** psycopg2 is synchronous — using it in async FastAPI forces thread pool usage, defeating the purpose of async. asyncpg is the native async PostgreSQL driver.

**Why UUID PKs over serial integers?** Distributed-safe, no enumeration attacks on IDs, works with multi-region inserts.

**Why JSONB for top_merchants?** Queryable with PostgreSQL JSONB operators (`@>`, `?`). Indexable. Not a raw TEXT column that requires application-level parsing.

**Why NullPool in Celery workers?** Celery uses `fork()` to spawn workers. SQLAlchemy connection pool objects are not fork-safe — shared file descriptors cause silent corruption. NullPool creates a fresh connection per operation.
