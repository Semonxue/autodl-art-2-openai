"""Bearer-token extraction from incoming requests."""

from __future__ import annotations

from fastapi import Request

from .errors import AuthError


def extract_bearer_token(request: Request) -> str:
    """Return the raw token from ``Authorization: Bearer <token>``.

    Strict mode: the scheme must be present (case-insensitive) and there
    must be exactly one whitespace run between ``Bearer`` and the token.
    Anything else is a 401 – we deliberately don't accept a bare token
    because OpenAI-style SDKs always emit ``Bearer``.
    """

    raw = (
        request.headers.get("authorization")
        or request.headers.get("Authorization")
        or ""
    )
    if not raw.strip():
        raise AuthError("Missing Authorization header")

    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError("Authorization header must be 'Bearer <token>'")

    token = parts[1].strip()
    if not token:
        raise AuthError("Empty bearer token")

    return token
