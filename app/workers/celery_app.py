"""
app/workers/celery_app.py

Celery factory with:
- Separate broker DB and result DB (no key collisions)
- Result expiry (no Redis OOM from tombstone accumulation)
- Priority queue routing (fast vs slow jobs)
- Task serialization hardened (json only, no pickle)
- Worker concurrency configured for I/O-bound LLM work
"""
from celery import Celery

from app.core.config import get_settings


def create_celery_app() -> Celery:
    settings = get_settings()

    broker_url = str(settings.redis_url).rstrip("/") + f"/{settings.celery_broker_db}"
    backend_url = str(settings.redis_url).rstrip("/") + f"/{settings.celery_result_db}"

    app = Celery("txn_pipeline")

    app.conf.update(
        # ── Broker / Backend ──────────────────────────────────────
        broker_url=broker_url,
        result_backend=backend_url,
        result_expires=settings.celery_result_expires,

        # ── Serialization (no pickle — security risk) ─────────────
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],

        # ── Task routing — fast queue vs slow LLM queue ───────────
        task_routes={
            "app.workers.tasks.process_csv_task": {"queue": "llm"},
        },
        task_default_queue="default",

        # ── Reliability ───────────────────────────────────────────
        task_acks_late=True,         # only ack after task completes (not before)
        task_reject_on_worker_lost=True,  # requeue if worker dies mid-task
        worker_prefetch_multiplier=1,     # don't pre-fetch; LLM tasks are slow

        # ── Timeouts ─────────────────────────────────────────────
        task_soft_time_limit=300,   # 5 min: triggers SoftTimeLimitExceeded
        task_time_limit=360,        # 6 min: hard kill

        # ── Beat schedule (cleanup) ───────────────────────────────
        beat_schedule={
            "cleanup-old-uploads": {
                "task": "app.workers.tasks.cleanup_old_uploads_task",
                "schedule": 3600,  # every hour
            }
        },
    )

    return app


celery_app = create_celery_app()
