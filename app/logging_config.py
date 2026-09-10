"""Structured JSON logging configuration and access-log middleware."""

from __future__ import annotations

import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .settings import Settings


# Fields we deliberately strip from the access log to avoid leaking secrets
# or producing log spam. Even token length is fine – never the token itself.
_DROP_REQUEST_HEADERS = {"authorization", "cookie", "set-cookie"}
_DROP_RESPONSE_HEADERS = {"set-cookie"}


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    # Standard LogRecord attributes we don't want to copy verbatim.
    _RESERVED = {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": int(time.time() * 1000),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Pull in any extra= keyword args the caller attached.
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(settings: Settings) -> None:
    """Install the JSON formatter on stderr and (optionally) a log file."""

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(settings.log_level)

    # stderr always gets the logs – systemd journal reads from here.
    stream_handler = logging.StreamHandler(stream=sys.stderr)
    stream_handler.setFormatter(JsonFormatter())
    root.addHandler(stream_handler)

    # Optional rotating file in the project folder.
    if settings.log_file:
        file_path = Path(settings.log_file)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            file_path,
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    # Tame chatty third-party loggers.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Write one structured access-log line per request."""

    def __init__(self, app, logger: logging.Logger) -> None:
        super().__init__(app)
        self._logger = logger

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        upstream_status: int | None = None
        status_code = 500  # default if the handler raises
        try:
            response = await call_next(request)
            status_code = response.status_code
            upstream_status = response.headers.get("x-upstream-status")
            return response
        finally:
            latency_ms = int((time.perf_counter() - start) * 1000)
            extra = {
                "method": request.method,
                "path": request.url.path,
                "status": status_code,
                "upstream_status": upstream_status,
                "latency_ms": latency_ms,
                "client": request.client.host if request.client else None,
            }
            self._logger.info("access", extra=extra)
