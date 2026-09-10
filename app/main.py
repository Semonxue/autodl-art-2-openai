"""FastAPI application factory and lifespan."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .cache import WorkflowCache
from .errors import UpstreamError, register_exception_handlers
from .logging_config import AccessLogMiddleware, configure_logging
from .routes import router as api_router
from .settings import Settings, load_settings
from .upstream import UpstreamClient


logger = logging.getLogger("autodl-openai-gateway")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app. Settings is overridable for tests."""

    settings = settings or load_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        timeout = httpx.Timeout(
            connect=settings.timeout_connect,
            read=settings.timeout_read,
            write=settings.timeout_write,
            pool=settings.timeout_pool,
        )
        limits = httpx.Limits(
            max_connections=settings.max_connections,
            max_keepalive_connections=settings.max_keepalive,
            keepalive_expiry=settings.keepalive_expiry,
        )
        async with httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            # The gateway is itself a forward proxy. We must not inherit
            # the developer's shell HTTP_PROXY / ALL_PROXY settings, or
            # we'd tunnel the user's bearer tokens through some random
            # proxy. The AutoDL API is reached directly.
            trust_env=False,
        ) as http:
            app.state.http = http
            app.state.upstream = UpstreamClient(
                http, base_url=settings.upstream_base_url
            )
            app.state.cache = WorkflowCache()
            app.state.settings = settings

            logger.info(
                "startup",
                extra={
                    "upstream_base_url": settings.upstream_base_url,
                    "timeout_connect": settings.timeout_connect,
                    "timeout_read": settings.timeout_read,
                    "has_bootstrap_token": bool(settings.bootstrap_token),
                    "skip_startup_sync": settings.skip_startup_sync,
                },
            )

            if settings.bootstrap_token and not settings.skip_startup_sync:
                try:
                    await app.state.cache.sync(
                        app.state.upstream, settings.bootstrap_token
                    )
                except UpstreamError as exc:
                    # Don't kill the gateway over a bad bootstrap token –
                    # the lazy path will retry per-request with the
                    # caller's token.
                    logger.warning(
                        "startup sync failed; falling back to lazy sync",
                        extra={"error": str(exc)},
                    )

            try:
                yield
            finally:
                logger.info("shutdown")

    app = FastAPI(
        title="AutoDL ComfyUI -> OpenAI Videos gateway",
        version="0.3.0",
        lifespan=lifespan,
    )

    # CORS: browser-based clients (web UIs) hit the gateway from a
    # different origin and need preflight + allow headers. Added last so
    # it sits outermost and answers OPTIONS preflight before routing.
    allowed_origins = [
        origin.strip()
        for origin in settings.cors_origins.split(",")
        if origin.strip()
    ] or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,  # auth is via Authorization header, not cookies
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_middleware(AccessLogMiddleware, logger=logger)
    register_exception_handlers(app)
    app.include_router(api_router)
    return app


# Module-level app for uvicorn.
app = create_app()


def main() -> None:
    """Entry point for ``python -m app``."""

    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.gateway_host,
        port=settings.gateway_port,
        workers=1,
        log_config=None,  # we configure logging ourselves
    )


if __name__ == "__main__":
    main()
