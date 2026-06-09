# Brutal Senior Engineer Code Review
## Alemeno Backend Assignment — AI-Powered Transaction Processing Pipeline

---

## Typical Intern Submission Score: **34 / 100**

---

## Category-by-Category Autopsy

### 1. Architecture (Score: 4/15) — FLAWED

**What interns do wrong:**

- **God-object `main.py`** — all models, routes, Celery tasks, and DB logic crammed into 2–3 files. No separation of concerns.
- **Synchronous DB calls inside async FastAPI handlers** — `session.query(...)` (SQLAlchemy ORM sync) called directly in `async def` routes. This blocks the event loop and kills concurrency. FastAPI becomes single-threaded.
- **Celery task is one 200-line function** — `process_csv_task()` does cleaning, anomaly detection, LLM calls, DB writes, and summary generation in a single monolith. Untestable, unretryable at step granularity.
- **No layering** — routes call DB directly, no service layer, no repository pattern. Changing DB means rewriting routes.
- **Circular imports** — `tasks.py` imports models, models import db, db imports config, config imports tasks. Breaks on startup.

**What it should be:**
```
app/
  api/routers/          ← HTTP boundary only, no business logic
  services/             ← orchestration logic
  workers/tasks.py      ← thin Celery shell calling services
  db/repository.py      ← all DB queries
  core/config.py        ← settings via pydantic-settings
```

---

### 2. Scalability (Score: 2/15) — CRITICAL FAILURES

**Failure point 1 — Full CSV loaded into RAM:**
```python
# Typical intern code
contents = await file.read()          # reads entire file into memory
df = pd.read_csv(io.BytesIO(contents)) # second full copy in RAM
```
At 100× scale with 10MB CSVs and 50 concurrent uploads: **~1GB RAM spike, OOM kill.**

**Fix:** Stream the file to disk first, pass the path to Celery, process in chunks.

**Failure point 2 — No connection pooling config:**
```python
engine = create_engine("postgresql://...")  # default pool_size=5, max_overflow=10
```
At 100× scale with 20 Celery workers each opening their own connections: **PostgreSQL hits its 100-connection limit and starts refusing.** Never configured `pool_size`, `max_overflow`, `pool_pre_ping`, or `pool_recycle`.

**Failure point 3 — One Celery task, no chunking:**
LLM classification batched naively — 15 uncategorised transactions in one 4000-token prompt. At scale with 5000-row CSVs: single prompt hits context limit, task crashes, entire job fails.

**Failure point 4 — Redis as both broker AND result backend:**
```python
app = Celery(broker='redis://redis:6379/0', backend='redis://redis:6379/0')
```
Result backend stores every task result in Redis forever. At scale: Redis OOM. Should use `result_expires=3600` and separate DB indices.

**Failure point 5 — No task routing/priority queues:**
All tasks on one queue. A 5000-row slow LLM job blocks a 10-row quick-clean job for 10 minutes.

---

### 3. Security (Score: 1/10) — DANGEROUS

**Issue 1 — Secrets in `docker-compose.yml`:**
```yaml
environment:
  - DATABASE_URL=postgresql://postgres:password123@db:5432/txndb
  - GEMINI_API_KEY=AIzaSy...actual_key_here...
```
Pushed to GitHub. Key immediately scraped by bots. Fined by Gemini ToS. **This happens 100% of the time with intern submissions.**

**Issue 2 — No file validation beyond extension:**
```python
if not filename.endswith('.csv'):
    raise HTTPException(400, "Not a CSV")
```
A malicious user uploads a 2GB file named `evil.csv`. Server runs out of disk. Or uploads a CSV with `=CMD()` formula injection. No size limit, no MIME-type check, no malware scan.

**Issue 3 — Path traversal on file storage:**
```python
filepath = f"uploads/{filename}"  # filename from user input!
open(filepath, 'wb')
```
Upload `../../../etc/cron.d/backdoor` as filename. Shell access in 60 seconds.

**Issue 4 — SQL injection via raw queries:**
Some interns use `f"SELECT * FROM transactions WHERE job_id = '{job_id}'"` to avoid ORM complexity. Classic.

**Issue 5 — No rate limiting on `/jobs/upload`:**
Endpoint accepts unlimited concurrent file uploads. DDoS trivial.

---

### 4. Docker Mistakes (Score: 3/10) — AMATEUR

**Mistake 1 — No `.dockerignore`:**
`COPY . .` copies `node_modules/`, `.git/`, `__pycache__/`, `venv/`, test fixtures. Image balloons to 2GB. Layer cache busted on every run.

**Mistake 2 — Running as root:**
```dockerfile
FROM python:3.11
# No USER directive
```
Container process runs as UID 0. Container escape = root on host.

**Mistake 3 — `pip install` in one layer with no cache mount:**
```dockerfile
COPY requirements.txt .
RUN pip install -r requirements.txt  # re-downloads everything on any code change
```

**Mistake 4 — No health checks:**
```yaml
services:
  api:
    depends_on:
      - db
      - redis
```
`depends_on` only waits for container *start*, not for Postgres to accept connections. API crashes on startup, needs `restart: always` as a bandage. Correct approach: `depends_on` with `condition: service_healthy`.

**Mistake 5 — No resource limits:**
One bad CSV job can eat all CPU/RAM on the Docker host, starving other containers.

**Mistake 6 — Development server in production image:**
```dockerfile
CMD ["uvicorn", "main:app", "--reload", "--host", "0.0.0.0"]
```
`--reload` watches filesystem and restarts on any file change. CPU waste and security hole.

---

### 5. Database Issues (Score: 3/10) — MISSING BASICS

**Issue 1 — No indexes:**
```sql
CREATE TABLE transactions (id SERIAL PRIMARY KEY, job_id UUID, ...);
```
`GET /jobs/{job_id}/results` does `SELECT * FROM transactions WHERE job_id = $1` — full table scan on every request. At 100k rows: 800ms query.

**Issue 2 — Alembic migrations absent:**
Tables created with `Base.metadata.create_all(engine)` on startup. No version history. Schema changes require manual `DROP TABLE`. Impossible to roll back.

**Issue 3 — No transaction handling:**
```python
db.add(transaction)
db.commit()  # called 95 times in a loop
```
95 individual commits instead of one batch. 95× the I/O. If it crashes at row 47, job is half-written with no rollback.

**Issue 4 — JSON stored as TEXT:**
```python
top_merchants = Column(String)  # stores '["Swiggy","Amazon","Flipkart"]'
```
Should be `JSONB` on PostgreSQL. Loses queryability, loses indexing, forces double-serialisation.

**Issue 5 — No soft deletes / audit trail:**
Job deletion is hard delete. No created_by, no updated_at triggers.

---

### 6. API Design Weaknesses (Score: 4/10) — SLOPPY

**Issue 1 — Wrong HTTP status codes:**
```python
return {"message": "Job created", "job_id": str(job.id)}  # returns 200
```
Should be `201 Created` with `Location: /jobs/{job_id}` header.

**Issue 2 — No pagination on `GET /jobs`:**
Returns all 10,000 jobs as one JSON blob. 50MB response.

**Issue 3 — No input validation on job_id:**
```python
@router.get("/jobs/{job_id}/status")
async def get_status(job_id: str, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
```
`job_id` is not validated as UUID. Passing `" OR 1=1--"` depends on ORM safety. Passing a 10000-char string hits the DB.

**Issue 4 — Inconsistent error response shape:**
Some routes return `{"detail": "..."}` (FastAPI default), others return `{"error": "..."}`, others return `{"message": "..."}`. No standard error envelope.

**Issue 5 — Missing `Content-Type` validation on upload:**
Accepts `multipart/form-data` but doesn't reject `application/json` POSTs gracefully.

**Issue 6 — No idempotency:**
Uploading the same file twice creates two identical jobs. No deduplication by content hash.

---

### 7. Celery Issues (Score: 3/10) — BROKEN AT SCALE

**Issue 1 — `bind=True` but no `self.retry()`:**
```python
@celery.task(bind=True, max_retries=3)
def process_csv(self, job_id):
    try:
        ...
    except Exception as e:
        raise  # raises instead of self.retry(exc=e, countdown=2**self.request.retries)
```
`max_retries` setting has no effect without explicit `self.retry()`.

**Issue 2 — No task state updates:**
Task goes from `PENDING` → `SUCCESS` with no intermediate updates. `GET /jobs/{id}/status` shows `pending` for 5 minutes then suddenly `completed`. No progress visibility.

**Issue 3 — Shared mutable state:**
```python
DOMESTIC_MERCHANTS = []  # module-level list mutated in tasks
```
Celery workers are forked processes. Mutations in one worker don't propagate. Race conditions.

**Issue 4 — No task deduplication:**
Submitting the same `job_id` to the queue twice (e.g. on network retry) processes it twice. Duplicate records in DB.

**Issue 5 — Celery beat not configured but described in README:**
README says "jobs auto-expire after 24 hours." No beat schedule. Stale jobs accumulate forever.

---

### 8. Redis Issues (Score: 3/10)

**Issue 1 — No Redis password:**
```yaml
redis:
  image: redis:7-alpine
  # no requirepass, no ACL
```
Redis port 6379 often accidentally exposed. Zero-auth by default.

**Issue 2 — No `maxmemory` policy:**
Redis fills up with Celery result tombstones and never evicts. OOM → Redis starts refusing writes → workers crash.

**Issue 3 — Using DB 0 for everything:**
Broker, result backend, and any application caching all on `redis://redis:6379/0`. Key collisions possible. Hard to flush selectively.

---

### 9. LLM Integration Weaknesses (Score: 3/10) — NAIVE

**Issue 1 — Prompt not deterministic:**
```python
prompt = f"Classify these transactions: {transactions}"
```
No temperature=0, no response format enforcement. LLM returns free prose instead of JSON 30% of the time. Parser crashes.

**Issue 2 — No response schema validation:**
```python
result = json.loads(llm_response)  # crashes if LLM returns markdown fences
categories = result["categories"]   # KeyError if schema differs
```

**Issue 3 — Retry logic is `time.sleep()` not exponential backoff:**
```python
for attempt in range(3):
    try:
        return call_llm(prompt)
    except:
        time.sleep(2)  # flat 2s, not exponential
```

**Issue 4 — LLM called with PII/raw data:**
Sending raw merchant names, amounts, account IDs to external LLM API. GDPR/DPDP Act violation. Should pseudonymise before sending.

**Issue 5 — No token budget management:**
With 15 uncategorised rows, the combined prompt is fine. With 500 rows, context window is blown. No chunking strategy.

**Issue 6 — API key hardcoded as fallback:**
```python
api_key = os.getenv("GEMINI_API_KEY", "AIzaSy_HARDCODED_FALLBACK")
```
Yes. This happens.

---

### 10. Missing Production Practices (Score: 8/15)

- No structured logging (`print()` everywhere, no correlation IDs)
- No OpenTelemetry / tracing
- No `pytest` tests at all — or 3 tests that `assert response.status_code == 200`
- No `pre-commit` hooks (black, ruff, mypy)
- No `CHANGELOG.md` or semantic versioning
- README curl examples use `localhost` and hardcoded UUIDs that don't exist
- No graceful shutdown handling (SIGTERM handler)
- No `/health` or `/ready` endpoints (Kubernetes can't probe it)
- No request ID middleware for log correlation

---

## Redesign: Target Score 95+/100

The following is the complete production-grade implementation.
