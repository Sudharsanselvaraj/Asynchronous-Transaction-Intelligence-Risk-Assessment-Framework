from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings


def _make_engine(*, nullpool: bool = False):
    settings = get_settings()
    kwargs: dict = {"echo": False}
    if nullpool:
        kwargs["poolclass"] = NullPool
    else:
        kwargs.update(
            {
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_recycle": settings.db_pool_recycle,
                "pool_pre_ping": True,
            }
        )
    return create_async_engine(str(settings.database_url), **kwargs)


# API process engine — uses connection pool
engine = _make_engine()
SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def get_async_session():
    async with SessionLocal() as session:
        yield session


def get_worker_engine():
    """NullPool engine for Celery workers — fork-safe, no shared file descriptors."""
    return _make_engine(nullpool=True)
