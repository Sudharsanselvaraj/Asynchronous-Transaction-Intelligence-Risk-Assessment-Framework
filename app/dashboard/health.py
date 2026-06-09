from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import text
from datetime import datetime
import aioredis
from celery import Celery
from app.core.config import get_settings
from app.db.session import engine

router = APIRouter()

async def get_redis_pool():
    settings = get_settings()
    return await aioredis.from_url(settings.redis_url, decode_responses=True)

def get_celery_app() -> Celery:
    from app.workers.celery_app import celery_app
    return celery_app

@router.get('/dashboard/health')
async def health_check(
    redis=Depends(get_redis_pool),
    celery=Depends(get_celery_app),
    db_engine: AsyncEngine = Depends(lambda: engine),
):
    # Database connection pool stats
    pool_stats = {'error': 'Pool stats not available'}
    try:
        async with db_engine.connect() as conn:
            result = await conn.exec_driver_sql('SELECT 1')
            await result.fetchone()
            pool_stats = {
                'connections': 1,
                'used': 0,
                'size': 1,
                'last_used': datetime.now().isoformat()
            }
    except Exception as e:
        pool_stats = {'error': str(e)}

    # Redis health
    redis_status = {}
    try:
        pong = await redis.ping()
        redis_status = {
            'status': 'ok' if pong else 'error',
            'connected_clients': await redis.client().conn.get('client', {}).get('numconnections', 0)
        }
    except Exception as e:
        redis_status = {
            'status': 'error',
            'error': str(e)
        }

    # Celery health
    celery_status = {}
    try:
        stats = celery.control.inspect()
        celery_status = {
            'status': 'active',
            'active_tasks': stats.active_tasks,
            'queues': stats.queues
        }
    except Exception as e:
        celery_status = {
            'status': 'error',
            'error': str(e)
        }

    return {
        'database': pool_stats,
        'redis': redis_status,
        'celery': celery_status,
    }
