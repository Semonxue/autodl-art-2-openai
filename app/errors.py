"""Typed application errors and their HTTP mapping.

Every error the gateway produces carries a stable ``type`` string so
clients can switch on it. The shape matches OpenAI's ``error`` object
so generic SDKs surface them sensibly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


@dataclass
class AppError(Exception):
    """Base class for all expected gateway errors."""

    message: str
    http_status: int
    error_type: str = "internal_error"
    extra_headers: dict[str, str] | None = None

    def to_response(self) -> JSONResponse:
        body: dict[str, Any] = {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": None,
                "code": None,
            }
        }
        return JSONResponse(
            status_code=self.http_status,
            content=body,
            headers=self.extra_headers or {},
        )


class AuthError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message=message,
            http_status=401,
            error_type="invalid_request_error",
            extra_headers={"WWW-Authenticate": "Bearer"},
        )


class InvalidRequest(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message=message,
            http_status=400,
            error_type="invalid_request_error",
        )


class UpstreamError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message=message,
            http_status=502,
            error_type="upstream_error",
        )


class UpstreamTimeout(AppError):
    def __init__(self, message: str = "Upstream timed out") -> None:
        super().__init__(
            message=message,
            http_status=504,
            error_type="upstream_error",
        )


class NotReadyError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message=message,
            http_status=404,
            error_type="invalid_request_error",
        )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire the ``AppError`` hierarchy into the FastAPI app."""

    @app.exception_handler(AppError)
    async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return exc.to_response()
