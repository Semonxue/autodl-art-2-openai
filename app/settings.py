"""Runtime configuration loaded from environment variables.

Every setting has a sane default so the gateway can be launched with zero
configuration. All knobs that affect production behaviour (listen
address, timeouts, pool sizes) live here – no magic numbers in code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name}={raw!r} is not an int") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name}={raw!r} is not a float") from exc


@dataclass(frozen=True)
class Settings:
    """Immutable view of all runtime knobs."""

    # --- server
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8765
    log_level: str = "INFO"

    # --- upstream
    upstream_base_url: str = "https://www.autodl.art/api/v1/comfyui"

    # --- workflow cache
    # If set, the gateway performs an eager schema sync at startup using
    # this token. If unset, the cache is populated lazily on the first
    # authenticated request that needs it.
    bootstrap_token: str | None = None
    # Skip the eager sync entirely (e.g. for offline tests or air-gapped
    # startup). The lazy path still runs when a real request arrives.
    skip_startup_sync: bool = False

    # --- CORS
    # Comma-separated list of allowed origins, or "*" to allow all.
    # Browser-based clients (web UIs) need this; native SDKs don't care.
    cors_origins: str = "*"

    # --- logging
    # Path to the rotating log file (relative to the working directory or
    # absolute). Empty string disables file logging (stderr only).
    log_file: str = "logs/gateway.log"
    log_max_bytes: int = 10 * 1024 * 1024  # 10 MB per file
    log_backup_count: int = 5

    # --- httpx timeouts (seconds)
    timeout_connect: float = 5.0
    timeout_read: float = 30.0
    timeout_submit: float = 60.0  # submit may upload ref images
    timeout_write: float = 10.0
    timeout_pool: float = 5.0

    # --- httpx connection pool
    max_connections: int = 100
    max_keepalive: int = 20
    keepalive_expiry: float = 30.0


def load_settings() -> Settings:
    return Settings(
        gateway_host=_env("GATEWAY_HOST", "0.0.0.0"),
        gateway_port=_env_int("GATEWAY_PORT", 8765),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        upstream_base_url=_env(
            "AUTODL_BASE_URL", "https://www.autodl.art/api/v1/comfyui"
        ),
        bootstrap_token=os.getenv("AUTODL_BOOTSTRAP_TOKEN") or None,
        skip_startup_sync=_env("SKIP_STARTUP_SYNC", "0").lower()
        in {"1", "true", "yes"},
        cors_origins=_env("CORS_ORIGINS", "*"),
        log_file=_env("LOG_FILE", "logs/gateway.log"),
        log_max_bytes=_env_int("LOG_MAX_BYTES", 10 * 1024 * 1024),
        log_backup_count=_env_int("LOG_BACKUP_COUNT", 5),
        timeout_connect=_env_float("TIMEOUT_CONNECT", 5.0),
        timeout_read=_env_float("TIMEOUT_READ", 30.0),
        timeout_submit=_env_float("TIMEOUT_SUBMIT", 60.0),
        timeout_write=_env_float("TIMEOUT_WRITE", 10.0),
        timeout_pool=_env_float("TIMEOUT_POOL", 5.0),
        max_connections=_env_int("MAX_CONNECTIONS", 100),
        max_keepalive=_env_int("MAX_KEEPALIVE", 20),
        keepalive_expiry=_env_float("KEEPALIVE_EXPIRY", 30.0),
    )
