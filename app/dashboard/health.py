from datetime import datetime

from fastapi import APIRouter
from redis.asyncio import from_url as redis_from_url
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import engine

router = APIRouter()


@router.get("/dashboard/health", tags=["ops"])
async def health_dashboard():
    settings = get_settings()
    result: dict = {"timestamp": datetime.utcnow().isoformat()}

    # Database
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        pool = engine.pool
        result["database"] = {
            "status": "ok",
            "pool_size": settings.db_pool_size,
            "checked_out": pool.checkedout() if hasattr(pool, "checkedout") else None,
        }
    except Exception as e:
        result["database"] = {"status": "error", "error": str(e)}

    # Redis
    try:
        redis = await redis_from_url(str(settings.redis_url), decode_responses=True)
        pong = await redis.ping()
        info = await redis.info("clients")
        await redis.aclose()
        result["redis"] = {
            "status": "ok" if pong else "error",
            "connected_clients": info.get("connected_clients"),
        }
    except Exception as e:
        result["redis"] = {"status": "error", "error": str(e)}

    # Celery — inspect is synchronous; do it in a thread to avoid blocking
    try:
        import asyncio
        from app.workers.celery_app import celery_app

        def _inspect():
            inspector = celery_app.control.inspect(timeout=2)
            active = inspector.active() or {}
            return {"status": "ok", "active_tasks": sum(len(v) for v in active.values())}

        celery_info = await asyncio.get_event_loop().run_in_executor(None, _inspect)
        result["celery"] = celery_info
    except Exception as e:
        result["celery"] = {"status": "error", "error": str(e)}

    return result
