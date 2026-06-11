"""
app/core/logging_config.py

Structured JSON logging — one JSON object per stdout line.
Compatible with Datadog, AWS CloudWatch Logs, Google Cloud Logging, and Grafana Loki.
No external dependencies; uses Python stdlib only.
"""
import json
import logging
import sys
from datetime import datetime, timezone

_SKIP_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "taskName", "thread", "threadName",
})


class JSONFormatter(logging.Formatter):
    """
    Emits one JSON object per log line.

    Callers pass structured context via extra={}:

        logger.info("llm call complete", extra={
            "job_id": job_id,
            "batch_size": len(batch),
            "duration_ms": 430,
        })

    Output:
        {"ts":"2024-09-04T10:01:23.456789+00:00","level":"INFO","logger":"app.services.llm",
         "msg":"llm call complete","job_id":"abc...","batch_size":5,"duration_ms":430}
    """

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        entry: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.message,
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        for k, v in record.__dict__.items():
            if k not in _SKIP_ATTRS and not k.startswith("_"):
                entry[k] = v
        return json.dumps(entry, default=str)


def setup_logging(level: str = "INFO") -> None:
    """
    Replace root logger handlers with a single structured JSON stdout handler.
    Safe to call multiple times — clears and resets each time.
    Call once at application startup before any loggers emit.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.addHandler(handler)

    # Suppress high-volume, low-signal library logs
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)
