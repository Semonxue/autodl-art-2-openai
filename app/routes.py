"""HTTP route handlers for the OpenAI-shaped gateway."""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .auth import extract_bearer_token
from .cache import WorkflowCache
from .errors import InvalidRequest, NotReadyError, UpstreamError
from .mapping import (
    build_upstream_body,
    coerce_field_types,
    extract_result_urls,
    map_size_to_resolution,
    video_object,
)
from .upstream import UpstreamClient


logger = logging.getLogger("autodl-openai-gateway")

router = APIRouter()


def _upstream(request: Request) -> UpstreamClient:
    return request.app.state.upstream


def _cache(request: Request) -> WorkflowCache:
    return request.app.state.cache


async def _ensure_cache_warm(request: Request, token: str) -> None:
    """Trigger a lazy sync the first time we need the cache.

    Concurrent first requests are coalesced via a single asyncio.Lock
    stored on ``app.state``.
    """

    cache = _cache(request)
    if len(cache) > 0:
        return

    lock: asyncio.Lock | None = getattr(request.app.state, "cache_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.cache_lock = lock

    async with lock:
        # Re-check inside the lock – another request may have populated.
        if len(cache) > 0:
            return
        await cache.sync(_upstream(request), token)


@router.get("/v1/models")
async def list_models(request: Request) -> JSONResponse:
    token = extract_bearer_token(request)
    await _ensure_cache_warm(request, token)

    items = _cache(request).all()
    now = int(time.time())
    data: list[dict] = []
    for entry in items:
        data.append(
            {
                "id": entry.workflow_id,
                "object": "model",
                "created": now,
                "owned_by": "autodl",
                "display_name": entry.name or entry.workflow_id,
            }
        )
    return JSONResponse({"object": "list", "data": data})


async def _parse_request_body(request: Request) -> dict:
    """Parse the request body as JSON or (fallback) form/multipart.

    Some OpenAI-compatible frontends (e.g. browser canvas apps) POST
    video-generation requests as multipart/form-data instead of JSON.
    We accept both: JSON is authoritative, form fields are flattened to
    a dict (last value wins). Uploaded files are left for the caller to
    handle (they map to reference images).
    """

    content_type = (request.headers.get("content-type") or "").lower()

    if "application/json" in content_type:
        try:
            body = await request.json()
        except ValueError as exc:
            raise InvalidRequest("Request body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise InvalidRequest("Request body must be a JSON object")
        return body

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        try:
            form = await request.form()
        except Exception as exc:
            logger.warning(
                "form parse failed",
                extra={"content_type": content_type, "error": str(exc)},
            )
            raise InvalidRequest("Could not parse form body") from exc
        body: dict = {}
        for key, value in form.multi_items():
            if isinstance(value, str):
                body[key] = value  # duplicate keys: last one wins
            # UploadFile values are skipped here; reference images are
            # handled separately once we know the exact field names.
        return body

    raw = await request.body()
    logger.warning(
        "unexpected content-type",
        extra={
            "content_type": content_type,
            "body_head": raw[:500].decode("utf-8", "replace"),
        },
    )
    raise InvalidRequest(
        f"Unsupported content-type {content_type!r}; expected application/json"
    )


@router.post("/v1/videos")
async def create_video(request: Request) -> JSONResponse:
    token = extract_bearer_token(request)
    await _ensure_cache_warm(request, token)

    body = await _parse_request_body(request)

    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise InvalidRequest("`model` (workflow id) is required")

    upstream_body = build_upstream_body(body)

    entry = _cache(request).get(model)

    # ``size`` arrives as an OpenAI ``WxH`` string (e.g. "480x480"), but
    # AutoDL resolution is a label ("480p(1:1)", "480p竖"...). Translate it
    # locally using the cached workflow whitelist, then validate.
    if entry:
        requested = upstream_body.get("resolution")
        if isinstance(requested, str) and entry.resolution_labels:
            if requested not in entry.resolution_labels:
                mapped = map_size_to_resolution(requested, entry.resolution_labels)
                if mapped:
                    upstream_body["resolution"] = mapped
                else:
                    raise InvalidRequest(
                        f"Invalid `size` {requested!r} for workflow {model!r}. "
                        f"Allowed values: {', '.join(entry.resolution_labels)}"
                    )

    # Whitelist filter: drop any field the workflow's input_rules does not
    # declare. Client-side UI fields (preset, resolution_name, shotType,
    # watermark, ...) must never reach AutoDL.
    if entry and entry.input_fields:
        upstream_body = {
            k: v for k, v in upstream_body.items() if k in entry.input_fields
        }

    # Coerce numeric fields (duration/seed) from client strings to the
    # upstream integer/float types.
    if entry and entry.field_types:
        coerce_field_types(upstream_body, entry.field_types)

    submitted = await _upstream(request).submit(
        token,
        model,
        upstream_body,
        timeout=request.app.state.settings.timeout_submit,
    )

    task_id = str(submitted.get("task_id", "")).strip()
    if not task_id:
        raise UpstreamError("Upstream response missing task_id")

    synthetic = dict(submitted)
    synthetic.setdefault("status", "QUEUED")
    return JSONResponse(
        status_code=202,
        content=video_object(
            task_id=task_id,
            model=model,
            upstream=synthetic,
            created_at=int(time.time()),
        ),
    )


@router.get("/v1/videos/{video_id}")
async def retrieve_video(request: Request, video_id: str) -> JSONResponse:
    token = extract_bearer_token(request)
    upstream = await _upstream(request).get_result(token, video_id)

    model = upstream.get("model")
    return JSONResponse(
        video_object(
            task_id=video_id,
            model=model if isinstance(model, str) else None,
            upstream=upstream,
            created_at=int(time.time()),
        )
    )


@router.get("/v1/videos/{video_id}/content")
async def video_content(request: Request, video_id: str):
    token = extract_bearer_token(request)
    upstream = await _upstream(request).get_result(token, video_id)
    urls = extract_result_urls(upstream.get("results"))
    if not urls:
        raise NotReadyError("Task has no downloadable result yet")
    return RedirectResponse(url=urls[0], status_code=302)


@router.get("/v1/workflows/{workflow_id}/schema")
async def workflow_schema(request: Request, workflow_id: str) -> JSONResponse:
    """Expose the cached schema so clients can discover legal parameters."""

    token = extract_bearer_token(request)
    await _ensure_cache_warm(request, token)

    entry = _cache(request).get(workflow_id)
    if entry is None:
        raise InvalidRequest(f"Unknown workflow: {workflow_id!r}")

    return JSONResponse(
        {
            "id": entry.workflow_id,
            "name": entry.name,
            "resolution_labels": list(entry.resolution_labels),
            "required_fields": list(entry.required_fields),
        }
    )


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
