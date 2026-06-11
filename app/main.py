"""
app/main.py

FastAPI application factory with:
- Structured JSON logging (app/core/logging_config.py)
- Request ID + latency logging middleware
- Global exception handler (standard error envelope)
- /health and /ready endpoints (Kubernetes liveness/readiness probes)
- CORS configured for production
"""
import time
import uuid
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.routers import jobs
from app.core.config import get_settings
from app.core.logging_config import setup_logging
from app.dashboard.health import router as health_router
from app.db.session import engine

# Configure structured logging before any logger emits
setup_logging(get_settings().log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify DB connectivity on startup; dispose pool on shutdown."""
    settings = get_settings()
    logger.info("startup", extra={"app": settings.app_name, "env": settings.app_env})
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("database connection verified")
    yield
    await engine.dispose()
    logger.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Transaction Processing Pipeline",
        description="AI-powered CSV transaction analysis with async job queue",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env != "production" else None,
    )

    # ── Request logging middleware (ID + latency) ────────────────────────
    @app.middleware("http")
    async def request_logger_middleware(request: Request, call_next):
        request_id = request.headers.get(settings.request_id_header, str(uuid.uuid4()))
        t0 = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        logger.info(
            "http",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": str(request.url.path),
                "status": response.status_code,
                "ms": duration_ms,
            },
        )
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
        logger.exception(
            "unhandled exception",
            extra={"method": request.method, "path": str(request.url.path)},
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": {"code": "INTERNAL_SERVER_ERROR", "message": "An unexpected error occurred"}},
        )

    # ── Routes ────────────────────────────────────────────────────────────
    app.include_router(jobs.router)
    app.include_router(health_router)

    # ── Health probes (Kubernetes liveness / readiness) ───────────────────
    @app.get("/health", tags=["ops"], summary="Liveness probe")
    async def health():
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"], summary="Readiness probe — checks DB")
    async def ready():
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return {"status": "ready", "database": "up"}
        except Exception as e:
            logger.error("readiness check failed", extra={"error": str(e)})
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not ready", "database": "down", "reason": str(e)},
            )

    return app


app = create_app()
