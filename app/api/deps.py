"""app/api/deps.py"""
from app.db.session import get_async_session

# Re-export for clean imports in routers
get_db = get_async_session
