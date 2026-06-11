# Architecture Diagrams

All diagrams are written in Mermaid and render natively on GitHub.
For draw.io, paste the diagram source at [draw.io](https://app.diagrams.net/) using `Extras > Edit Diagram`.

---

## 1. System Component Diagram

```mermaid
graph TB
    subgraph Client["Client Layer"]
        CL["curl / Postman / Browser"]
    end

    subgraph API["FastAPI — port 8000"]
        direction TB
        UP["POST /jobs/upload"]
        ST["GET /jobs/{id}/status"]
        RS["GET /jobs/{id}/results"]
        LS["GET /jobs"]
        HL["GET /health  GET /ready"]
        DH["GET /dashboard/health"]
    end

    subgraph DataLayer["Data Layer"]
        direction TB
        PG[("PostgreSQL\n─────────────\njobs\ntransactions\njob_summaries")]
        RD[("Redis\n─────────────\ndb0: broker\ndb1: results")]
        FS["/tmp/txn_uploads\n(CSV files)"]
    end

    subgraph Workers["Background Workers"]
        direction TB
        W["Celery Worker\n─────────────────\n① clean_csv()\n② detect_anomalies()\n③ classify_uncategorised()\n④ generate_narrative()\n⑤ bulk_insert()"]
        B["Celery Beat\n─────────────\ncleanup_old_uploads\n(every hour)"]
    end

    subgraph External["External Services"]
        G["Gemini 1.5 Flash\ngenerativelanguage\n.googleapis.com"]
    end

    CL -->|"HTTP"| UP
    CL -->|"HTTP poll"| ST
    CL -->|"HTTP"| RS
    CL -->|"HTTP"| LS

    UP -->|"stream chunks"| FS
    UP -->|"INSERT Job"| PG
    UP -->|"LPUSH task"| RD

    ST -->|"SELECT Job + Summary"| PG
    RS -->|"SELECT Job + Transactions"| PG
    LS -->|"SELECT Jobs"| PG

    W -->|"BRPOP"| RD
    W -->|"READ csv"| FS
    W -->|"INSERT Transactions\nUPSERT Summary\nUPDATE Job"| PG
    W -->|"POST (3x retry\nexp backoff)"| G

    B -->|"DELETE old CSVs"| FS

    style PG fill:#336791,color:#fff
    style RD fill:#DC382D,color:#fff
    style G fill:#4285F4,color:#fff
```

---

## 2. Request Sequence Diagram — Upload to Result

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant API as FastAPI
    participant DB as PostgreSQL
    participant R as Redis
    participant W as Celery Worker
    participant G as Gemini API

    C->>API: POST /jobs/upload (multipart/form-data)
    API->>API: Validate MIME type, extension, file size
    API->>API: Stream to disk in 64KB chunks + compute SHA-256
    API->>DB: SELECT Job WHERE file_hash = ? (dedup check)
    DB-->>API: null (new file)
    API->>DB: INSERT Job(status=pending)
    DB-->>API: job.id (UUID)
    API->>R: LPUSH llm-queue [job_id, file_path]
    API-->>C: 201 {"job_id":"550e8400-...","status":"pending"}

    Note over C,API: Client polls every 2–5 seconds

    loop until status = completed | failed
        C->>API: GET /jobs/{job_id}/status
        API->>DB: SELECT Job LEFT JOIN JobSummary
        DB-->>API: {status: "processing", summary: null}
        API-->>C: {status: "processing"}
    end

    Note over W,G: Async background processing

    W->>R: BRPOP llm-queue (blocking pop)
    W->>DB: UPDATE Job SET status=processing
    W->>W: clean_csv() — normalise dates, strip $, uppercase, dedup
    Note right of W: 95 raw → 85 clean, 10 duplicates removed

    W->>W: detect_anomalies() — 4 rules applied per row
    Note right of W: 22 anomalies flagged

    W->>G: classify_uncategorised() — batch 1 (13 rows)
    Note right of W: Only merchant, amount, currency sent (no PII)
    G-->>W: {"classifications":[{"index":0,"category":"Food"},...]}
    W->>W: Validate response with Pydantic ClassificationResponse

    W->>G: generate_narrative_summary() — single call with aggregates
    G-->>W: {"narrative":"...","risk_level":"high","top_merchants":[...]}
    W->>W: Validate response with Pydantic NarrativeSummaryResponse

    W->>DB: INSERT 85 Transactions (bulk, single commit)
    W->>DB: UPSERT JobSummary
    W->>DB: UPDATE Job SET status=completed, row_count_raw=95, row_count_clean=85
    W->>R: ACK task (task_acks_late=True — only now removed from queue)
    W->>W: DELETE csv file from disk

    C->>API: GET /jobs/{job_id}/results
    API->>DB: SELECT Job + Transactions + Summary (eager load via selectinload)
    DB-->>API: Full result set
    API->>API: Build response (anomalies filter, category_breakdown aggregation)
    API-->>C: 200 {transactions[], anomalies[], category_breakdown[], summary{}}
```

---

## 3. Entity-Relationship Diagram

```mermaid
erDiagram
    Job {
        uuid id PK "UUID v4, not enumerable"
        varchar filename "Full path on disk"
        varchar original_filename "Original upload name"
        varchar file_hash "SHA-256 — indexed for dedup"
        enum status "pending|processing|completed|failed"
        int row_count_raw "Set after cleaning"
        int row_count_clean "Set after cleaning"
        text error_message "Set on failure"
        varchar celery_task_id "For task introspection"
        timestamptz created_at "DB clock (server_default)"
        timestamptz updated_at "DB clock"
        timestamptz completed_at "Set on terminal status"
    }

    Transaction {
        uuid id PK
        uuid job_id FK "CASCADE DELETE"
        varchar txn_id "Original or GEN-{hex8}"
        varchar date "ISO 8601 after cleaning"
        varchar merchant
        float amount "Cleaned; null if unparseable"
        varchar currency "INR|USD after normalisation"
        varchar status "SUCCESS|FAILED|PENDING"
        varchar category "Cleaned or LLM-assigned"
        varchar account_id
        text notes "Original free text"
        bool is_anomaly "Any rule triggered"
        text anomaly_reason "Semicolon-separated reasons"
        varchar llm_category "Set only if LLM classified"
        text llm_raw_response "Raw LLM JSON for audit"
        bool llm_failed "True if all retries exhausted"
        timestamptz created_at
    }

    JobSummary {
        uuid id PK
        uuid job_id FK "UNIQUE — one summary per job"
        float total_spend_inr
        float total_spend_usd
        jsonb top_merchants "Array: queryable with @> operator"
        jsonb category_breakdown "Object: {Food: 12000.0, ...}"
        int anomaly_count
        text narrative "LLM-generated 2-3 sentences"
        varchar risk_level "low|medium|high"
        bool llm_failed "True if narrative generation failed"
        timestamptz created_at
    }

    Job ||--o{ Transaction : "one job has many transactions\n(CASCADE DELETE)"
    Job ||--o| JobSummary : "one job has one summary\n(CASCADE DELETE)"
```

---

## 4. Data Flow — What Happens Inside the Worker

```mermaid
flowchart TD
    A["Dequeue task\njob_id + file_path"] --> B["UPDATE Job\nstatus=processing"]
    B --> C["clean_csv(path)\nPure function"]
    C --> D{"parse errors?"}
    D -->|"missing required columns"| E["UPDATE Job\nstatus=failed\nerror_message=..."]
    D -->|"success"| F["detect_anomalies(rows)\nPure function — 4 rules"]
    F --> G["classify_uncategorised(rows)\nBatched LLM calls"]
    G --> H{"LLM available?"}
    H -->|"all retries failed"| I["Mark batch\nllm_failed=true\ncontinue"]
    H -->|"success"| J["Validate response\nPydantic schema"]
    I --> K
    J --> K["generate_narrative_summary()\nSingle LLM call"]
    K --> L["bulk_insert(Transactions)\n85 rows, 1 commit"]
    L --> M["upsert(JobSummary)"]
    M --> N["UPDATE Job\nstatus=completed\nrow_counts set"]
    N --> O["DELETE csv from disk\nACK Celery task"]
```

---

## 5. Anomaly Detection Rules

```mermaid
flowchart LR
    subgraph Input["Per-Row Input"]
        R["CleanedRow\namount, currency,\nstatus, merchant,\naccount_id, notes"]
    end

    subgraph Rules["4 Independent Rules"]
        direction TB
        R1["Statistical Outlier\namount > 3× account median\n(SUCCESS rows only)"]
        R2["Currency Mismatch\nUSD at domestic merchant\nSwiggy, Ola, IRCTC, Zomato, Jio"]
        R3["High-Value Failed\nstatus=FAILED\nAND amount > ₹5,000"]
        R4["Source Annotation\nnotes contains 'suspicious'\n(case-insensitive)"]
    end

    subgraph Output["AnomalyResult"]
        O1["is_anomaly=False\nreason=null"]
        O2["is_anomaly=True\nreason=semicolon-joined\nmatching rule descriptions"]
    end

    R --> R1
    R --> R2
    R --> R3
    R --> R4

    R1 -->|"no match"| O1
    R2 -->|"no match"| O1
    R3 -->|"no match"| O1
    R4 -->|"no match"| O1

    R1 -->|"matches"| O2
    R2 -->|"matches"| O2
    R3 -->|"matches"| O2
    R4 -->|"matches"| O2
```

---

## 6. Scalability Bottleneck Map

```mermaid
graph LR
    subgraph "100x Traffic"
        T["100x concurrent\nuploads + polls"]
    end

    subgraph "Bottleneck 1 — First to break"
        B1["PostgreSQL connections\nmax ~100\n20 workers × pool_size=5"]
        S1["PgBouncer\n1000 app → 100 DB"]
    end

    subgraph "Bottleneck 2"
        B2["Gemini rate limits\n15 req/min free tier"]
        S2["Redis rate limiter\n+ paid tier"]
    end

    subgraph "Bottleneck 3"
        B3["Upload disk\nsingle Docker volume\nfills with large CSVs"]
        S3["S3 / MinIO\nobject storage"]
    end

    subgraph "Bottleneck 4"
        B4["Worker backlog\n4 concurrent tasks"]
        S4["Scale horizontally\ndocker compose scale worker=10"]
    end

    T -->|"breaks first"| B1
    B1 -->|"mitigated by"| S1
    T -->|"breaks second"| B2
    B2 -->|"mitigated by"| S2
    T -->|"breaks third"| B3
    B3 -->|"mitigated by"| S3
    T -->|"slow degradation"| B4
    B4 -->|"mitigated by"| S4

    style B1 fill:#DC382D,color:#fff
    style B2 fill:#FF6B35,color:#fff
    style B3 fill:#FFA500,color:#000
    style B4 fill:#90EE90,color:#000
```
