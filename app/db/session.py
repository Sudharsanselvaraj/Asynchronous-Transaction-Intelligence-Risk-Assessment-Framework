from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(str(settings.database_url))
SessionLocal = sessionmaker(engine, class_=AsyncSession)

async def get_async_session():
    async with SessionLocal() as session:
        yield session