txn-pipeline/
├── app/
│   ├── api/
│   │   ├── __init__.py
│   │   ├── deps.py               # FastAPI dependency injection
│   │   └── routers/
│   │       ├── __init__.py
│   │       └── jobs.py           # All /jobs/* endpoints
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py             # pydantic-settings, all env vars
│   │   ├── logging.py            # structlog setup
│   │   └── errors.py             # global error handlers + standard envelope
│   ├── db/
│   │   ├── __init__.py
│   │   ├── session.py            # async SQLAlchemy engine + session factory
│   │   └── repository.py         # all DB queries, zero SQL in routes
│   ├── models/
│   │   ├── __init__.py
│   │   └── models.py             # SQLAlchemy ORM models
│   ├── schemas/
│   │   ├── __init__.py
│   │   └── schemas.py            # Pydantic v2 request/response schemas
│   ├── services/
│   │   ├── __init__.py
│   │   ├── cleaning.py           # data cleaning logic
│   │   ├── anomaly.py            # anomaly detection logic
│   │   └── llm.py                # LLM integration with retry + validation
│   ├── workers/
│   │   ├── __init__.py
│   │   ├── celery_app.py         # Celery factory, queue routing
│   │   └── tasks.py              # thin Celery tasks, calls services
│   └── main.py                   # FastAPI app factory
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 001_initial.py
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_cleaning.py
│   │   ├── test_anomaly.py
│   │   └── test_llm.py
│   └── integration/
│       └── test_jobs_api.py
├── infra/
│   └── postgres/
│       └── init.sql
├── .dockerignore
├── .env.example
├── .pre-commit-config.yaml
├── Dockerfile
├── docker-compose.yml
├── alembic.ini
├── pyproject.toml
└── README.md
