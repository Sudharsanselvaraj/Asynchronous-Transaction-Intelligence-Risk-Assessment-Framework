"""
app/main.py

FastAPI application factory with:
- Request ID middleware (log correlation)
- Structured logging
- Global exception handler (standard error envelope)
- /health and /ready endpoints (Kubernetes probes)
- CORS configured for production
- No --reload in production
"""
import uuid
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from app.dashboard.health import router as health_router
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.routers import jobs
from app.core.config import get_settings
from app.db.session import engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run DB connectivity check on startup."""
    settings = get_settings()
    logger.info("Starting %s in %s mode", settings.app_name, settings.app_env)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("Database connection verified")
    yield
    await engine.dispose()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Transaction Processing Pipeline",
        description="AI-powered CSV transaction analysis with async job queue",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env != "production" else None,
    )

    # ── Request ID middleware ────────────────────────────────────────────
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get(
            settings.request_id_header, str(uuid.uuid4())
        )
        response = await call_next(request)
        response.headers[settings.request_id_header] = request_id
        return response

    # ── CORS ─────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"] if settings.app_env != "production" else [],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # ── Global exception handler ──────────────────────────────────────────
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception on %s %s", request.method, request.url)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "An unexpected error occurred",
                }
            },
        )

    # ── Routes ────────────────────────────────────────────────────────────
    app.include_router(jobs.router)
    app.include_router(health_router)

    # ── Health probes ─────────────────────────────────────────────────────
    @app.get("/health", tags=["ops"], summary="Liveness probe")
    async def health():
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"], summary="Readiness probe")
    async def ready():
        """Checks DB connectivity before accepting traffic."""
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return {"status": "ready"}
        except Exception as e:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not ready", "reason": str(e)},
            )

    return app


app = create_app()
