"""Async client for the AutoDL ComfyUI HTTP API.

Owns a process-shared :class:`httpx.AsyncClient` so we get TCP keep-alive
and a connection pool. Methods accept the per-request bearer token and
inject it as the ``Authorization`` header (no caching of tokens).
"""

from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from .errors import UpstreamError, UpstreamTimeout


class UpstreamClient:
    """Thin async wrapper around the AutoDL ComfyUI endpoints."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")

    @property
    def base_url(self) -> str:
        return self._base_url

    # ----------------------------------------------------------- public API

    async def list_workflows(self, token: str) -> dict[str, Any]:
        """Return ``data.list`` from the workflow catalog endpoint."""

        response = await self._request(
            "POST",
            f"{self._base_url}/workflows",
            token,
            json_body={},
        )
        return response

    async def submit(
        self,
        token: str,
        workflow_id: str,
        body: Mapping[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Submit a workflow run. ``timeout`` overrides the per-client read
        timeout (useful for image-to-video workflows that need to upload
        the reference media to AutoDL's storage)."""

        workflow_id = (workflow_id or "").strip()
        if not workflow_id:
            raise UpstreamError("workflow_id is empty")
        response = await self._request(
            "POST",
            f"{self._base_url}/comfyui_workflow/{quote(workflow_id, safe='')}",
            token,
            json_body=dict(body),
            timeout=timeout,
        )
        task_id = str(response.get("task_id", "")).strip()
        if not task_id:
            raise UpstreamError(
                "AutoDL submit response did not contain task_id: "
                + _json_excerpt(response)
            )
        return response

    async def get_result(self, token: str, task_id: str) -> dict[str, Any]:
        task_id = (task_id or "").strip()
        if not task_id:
            raise UpstreamError("task_id is empty")
        return await self._request(
            "GET",
            f"{self._base_url}/comfyui_workflow/result/{quote(task_id, safe='')}",
            token,
        )

    async def get_workflow(self, token: str, workflow_id: str) -> dict[str, Any]:
        """Return the schema / metadata for a single workflow.

        Used at startup to populate the local workflow cache. Per-request
        submissions must NOT call this – the cache absorbs the cost.
        """

        workflow_id = (workflow_id or "").strip()
        if not workflow_id:
            raise UpstreamError("workflow_id is empty")
        return await self._request(
            "GET",
            f"{self._base_url}/workflows/{quote(workflow_id, safe='')}",
            token,
        )

    # ------------------------------------------------------------- internals

    async def _request(
        self,
        method: str,
        url: str,
        token: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            response = await self._http.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout(f"Upstream timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Could not reach upstream: {exc}") from exc

        if response.status_code >= 400:
            body = response.text[:1000].strip()
            raise UpstreamError(
                f"Upstream returned HTTP {response.status_code}: {body}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError(
                "Upstream returned non-JSON data: " + response.text[:1000].strip()
            ) from exc

        if not isinstance(payload, dict):
            raise UpstreamError(
                "Upstream returned JSON that is not an object: "
                + _json_excerpt(payload)
            )

        return _unwrap(payload)


def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept both the ``{code, data}`` envelope and bare JSON."""

    if "code" not in payload:
        return payload

    code = str(payload.get("code", "")).strip()
    if code.lower() not in {"success", "ok", "200"}:
        message = payload.get("message") or payload.get("msg") or code
        raise UpstreamError(f"Upstream rejected the request: {message}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise UpstreamError(
            "Upstream success response did not contain an object in data: "
            + _json_excerpt(payload)
        )
    return data


def _json_excerpt(value: Any, limit: int = 1000) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = repr(value)
    return rendered[:limit]
