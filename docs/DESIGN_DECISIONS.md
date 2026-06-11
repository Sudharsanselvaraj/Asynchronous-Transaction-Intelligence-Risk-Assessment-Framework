# Design Decisions

This document explains every significant technical choice made in this system, including the alternatives that were considered and rejected. The goal is to demonstrate that design choices are deliberate rather than default.

---

## 1. FastAPI over Django REST Framework

**Decision:** FastAPI

**Why:**

FastAPI is async-native — every endpoint can be `async def` without thread-pool workarounds. In this system, every request touches the database (`async with session`) and the upload endpoint reads a file off disk. With DRF, these would block the server's thread pool on every concurrent request. FastAPI + asyncpg gives genuine non-blocking I/O.

Additional benefits:
- Pydantic v2 is the validation layer — request schemas, response schemas, and LLM output validation all use the same library and the same mental model
- `/docs` and `/redoc` are auto-generated from route decorators; zero extra work for the reviewer
- `Depends()` injection cleanly separates the DB session from route logic — routes are orchestration only

**Tradeoffs:**
- DRF has a larger third-party ecosystem (django-filter, drf-spectacular, django-guardian)
- FastAPI async exception handling is slightly less battle-tested than DRF's synchronous equivalent
- DRF admin UI is out-of-the-box; FastAPI has no equivalent

**Alternatives rejected:**
- **DRF**: requires `asgiref.sync_to_async` wrappers for every async ORM call, which negates the benefit
- **Starlette (bare)**: more control, but too minimal — would need to re-implement validation, serialisation, and dependency injection

---

## 2. PostgreSQL over alternatives

**Decision:** PostgreSQL 16

**Why:**

Three specific PostgreSQL features were the deciding factors:

1. **JSONB** — `top_merchants` and `category_breakdown` in `JobSummary` are stored as JSONB, not TEXT. This means they are queryable with `@>` and `?` operators and indexable with GIN indexes. A TEXT column would require parsing on every read.

2. **ACID transactions** — job status transitions (`pending → processing → completed`) must be atomic. The sequence: insert transactions → upsert summary → update job status must either all succeed or all roll back. Eventual consistency (MongoDB-style) would allow a "completed" job with missing transaction rows.

3. **UUID primary keys** — the `uuid-ossp` extension and `UUID` column type are first-class in PostgreSQL. UUID PKs prevent enumeration attacks on job IDs and are safe for distributed inserts across multiple API instances.

**Tradeoffs:**
- Heavier operational profile than SQLite for this scale
- Schema migrations require Alembic coordination; can't just `CREATE TABLE` ad-hoc
- No native horizontal write scaling (requires Citus or similar)

**Alternatives rejected:**
- **MongoDB**: BSON documents are flexible but ACID is collection-scoped only; cannot atomically update two collections
- **SQLite**: single-writer limit makes it unsuitable for shared API + worker access; asyncpg doesn't support it
- **MySQL**: weaker JSONB support; no `uuid-ossp`; different `ON CONFLICT` semantics

---

## 3. Celery + Redis over alternatives

**Decision:** Celery 5.4 with Redis as both broker and result backend

**Why:**

The core requirement is that a job survives a worker crash mid-processing. Celery provides this via two config flags:

```python
task_acks_late=True          # Task NOT acknowledged until it finishes
task_reject_on_worker_lost=True  # Re-queued if worker process dies
```

Without these, a worker that dies after dequeuing but before completing would silently lose the job. With them, Redis re-delivers the task automatically.

Additional reasoning:
- **Separate broker/result databases** (`celery_broker_db=0`, `celery_result_db=1`) — prevents Celery result tombstones from colliding with broker queue keys
- **Beat scheduler** — the cleanup task (`cleanup_old_uploads_task`) runs hourly without needing a separate cron container
- **Priority queues** — `llm` queue for slow Gemini-calling tasks, `default` for fast operations; allows independent scaling

**Tradeoffs:**
- Adds Redis as a required infrastructure dependency
- Celery workers use `fork()` — SQLAlchemy connection pools are NOT fork-safe (shared OS file descriptors cause silent corruption). Mitigated with `NullPool` in worker processes.
- Celery monitoring (Flower) is a separate service

**Alternatives rejected:**
- **FastAPI BackgroundTasks**: No persistence — a process restart loses all queued work
- **RQ (Redis Queue)**: Simpler API but no built-in `task_acks_late` equivalent; retry semantics require manual implementation
- **Celery with RabbitMQ**: More durable (persistent queues, dead letter exchanges) but significantly heavier to run in Docker Compose for a development context

---

## 4. Gemini 1.5 Flash over other LLMs

**Decision:** Gemini 1.5 Flash via REST API

**Why:**

1. **Free tier** — No credit card required; no spend incurred during development or review
2. **`responseMimeType: "application/json"`** — Forces the model to output raw JSON, not Markdown-wrapped JSON. Eliminates the brittle fence-stripping pattern (though the code strips fences defensively anyway)
3. **Speed** — Flash variant is optimised for throughput; classification prompts complete in under 2 seconds
4. **Pydantic validation on output** — Every LLM response is validated by `ClassificationResponse` or `NarrativeSummaryResponse` before any value touches the database. Invalid categories are coerced to `"Other"` at the validator level, not via ad-hoc `if/else`

**Tradeoffs:**
- Vendor lock-in to Google; but the integration is isolated to `_call_gemini()` — swapping to OpenAI requires changing one function
- Free tier has rate limits (15 requests/minute on Flash); at 100x load this becomes the first bottleneck
- Gemini does not have a streaming mode used here (not needed for batch classification)

**Alternatives rejected:**
- **OpenAI GPT-4o-mini**: Good quality but requires paid credits; not suitable for a zero-spend submission
- **Ollama (local)**: Eliminates external dependency but requires GPU or very slow CPU inference inside Docker; impractical for a Compose setup

---

## 5. Repository Pattern

**Decision:** All database queries isolated in `app/db/repository.py`

**Why:**

Routes and services never import `sqlalchemy` directly. Every DB interaction goes through a repository class (`JobRepository`, `TransactionRepository`, `SummaryRepository`).

This gives three concrete benefits:

1. **Testability** — service logic can be tested with a mock repository, without a real database (future integration tests will exploit this)
2. **Single caching insertion point** — adding Redis read-through caching for `get_with_summary()` requires changing only the repository, not the route
3. **Query isolation** — every SQL statement is in one file; optimising a slow query means looking in one place, not grep-ing across routes

**Tradeoffs:**
- Extra layer of indirection for small queries
- For a very simple CRUD API, the repository pattern adds boilerplate without immediate payoff

---

## 6. NullPool for Celery Workers

**Decision:** `poolclass=NullPool` when creating the SQLAlchemy engine inside Celery workers

**Why:**

Celery uses Python's `os.fork()` to spawn worker processes. A SQLAlchemy connection pool holds OS-level socket file descriptors. When a process forks, both the parent and child inherit the same socket — two processes sharing one TCP connection to PostgreSQL. PostgreSQL will see corrupted protocol messages and close the connection.

`NullPool` creates a fresh TCP connection for every `async with session:` block and closes it on exit. No file descriptors are held across the fork boundary. The trade-off is slightly higher connection overhead per task, which is acceptable because Celery tasks are long-running relative to a single DB roundtrip.

**Reference:** [SQLAlchemy documentation on using connection pools with multiprocessing](https://docs.sqlalchemy.org/en/20/core/pooling.html#using-connection-pools-with-multiprocessing-or-os-fork)

---

## 7. SHA-256 File Deduplication

**Decision:** Hash the uploaded file content; return the existing job if a match is found

**Why:**

Without deduplication, a reviewer or client that uploads the same file twice would:
1. Consume two Gemini API quota slots
2. Create two identical job records
3. Store 85 duplicate transaction rows

The hash is computed while streaming the file to disk (same pass), so there is zero additional I/O cost.

**Tradeoffs:**
- A single changed byte (e.g., trailing newline) produces a different hash; two semantically identical CSVs with different whitespace are treated as different
- The hash is computed after streaming the entire file; for very large files this delays the hash computation slightly

---

## 8. Exponential Backoff on LLM Retries

**Decision:** Retry LLM calls up to `LLM_RETRY_MAX=3` times with delay `LLM_RETRY_BASE_DELAY * (2 ** attempt)`

**Why:**

Gemini rate-limit responses (`429 Too Many Requests`) are transient — a fixed delay would re-hit the rate limit at the same time across all workers. Exponential backoff spreads retries out:

- Attempt 1 failure → wait 1s
- Attempt 2 failure → wait 2s
- Attempt 3 failure → wait 4s → raise `RuntimeError`

After all retries fail, rows in that batch are marked `llm_failed=true` and the job continues. The entire job does NOT fail because of an LLM API outage — this is a deliberate tradeoff: partial data with `llm_failed` flags is more useful than a hard job failure with no output.

**Tradeoffs:**
- Does not distinguish retryable (`429`, `503`) from non-retryable (`401`, `400`) errors — all errors are retried
- Maximum retry duration: 1 + 2 + 4 = 7 seconds per batch; acceptable for a background job with no SLA

---

## 9. Streaming Upload to Disk

**Decision:** Read uploaded file in 64KB chunks, write to disk; never call `await file.read()` without a size limit

**Why:**

If a 10MB file is loaded entirely into RAM with `await file.read()`, and 50 concurrent uploads arrive simultaneously, the API process holds 500MB in memory — likely causing OOM. The chunked approach:
1. Reads 64KB at a time
2. Writes each chunk to disk
3. Accumulates the SHA-256 hash incrementally
4. Enforces the `max_upload_bytes` limit mid-stream (no need to read the full file to reject it)

**Tradeoffs:**
- Disk I/O instead of memory — this is the right trade because disk is cheap and bounded, RAM under concurrent load is not

---

## 10. Async SQLAlchemy with asyncpg

**Decision:** `create_async_engine` + `asyncpg` driver instead of synchronous `psycopg2`

**Why:**

FastAPI runs on an async event loop. If a DB query uses `psycopg2` (synchronous), the event loop thread blocks for the entire duration of the query. No other requests can be served while one request waits for PostgreSQL. With `asyncpg`, the event loop yields control during DB waits and serves other requests concurrently.

**Tradeoffs:**
- `psycopg2` has a larger community and more Stack Overflow answers
- `asyncpg` does not support every PostgreSQL feature (e.g., `COPY` protocol requires explicit handling)
- Debugging async SQLAlchemy issues is harder than sync equivalents
