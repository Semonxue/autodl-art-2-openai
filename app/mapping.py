"""Pure helpers for translating between OpenAI shape and AutoDL shape.

These functions are intentionally free of any I/O, framework imports, or
global state so they can be unit-tested in isolation and reused from
both the request and the test fixtures.
"""

from __future__ import annotations

import base64
from typing import Any, Mapping


# Statuses from the AutoDL side that we treat as terminal failures.
_FAILURE_STATUSES = {
    "FAILED",
    "FAILURE",
    "ERROR",
    "CANCELLED",
    "CANCELED",
}


def map_status(raw_status: Any) -> str:
    """Map an AutoDL status string to the OpenAI Videos status enum."""

    if not raw_status:
        return "queued"
    normalized = str(raw_status).upper().strip()
    if normalized in {"QUEUED", "PENDING"}:
        return "queued"
    if normalized in {"RUNNING", "PROCESSING", "IN_PROGRESS"}:
        return "in_progress"
    if normalized in {"SUCCESS", "SUCCEEDED", "COMPLETED"}:
        return "completed"
    if normalized in _FAILURE_STATUSES:
        return "failed"
    # Unknown non-terminal – keep clients polling.
    return "in_progress"


def build_upstream_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI Videos request body to an AutoDL body.

    Rules:
        * ``model`` is consumed by the URL path, never forwarded.
        * ``prompt`` is forwarded as-is.
        * ``seconds`` becomes ``duration`` only if the body doesn't
          already contain ``duration`` (explicit wins).
        * ``size`` becomes ``resolution`` only if the body doesn't
          already contain ``resolution``.
        * ``metadata`` (object) is merged into the upstream body.
          Top-level keys win on collision.
        * Everything else is forwarded verbatim, including image URLs
          or base64 references – the upstream workflow decides what
          to do with them.
    """

    upstream: dict[str, Any] = {}

    for key, value in body.items():
        if key == "model":
            continue
        if key == "seconds":
            upstream.setdefault("duration", value)
            continue
        if key == "size":
            upstream.setdefault("resolution", value)
            continue
        if key == "metadata":
            if isinstance(value, Mapping):
                for meta_key, meta_value in value.items():
                    upstream.setdefault(meta_key, meta_value)
            continue
        upstream[key] = value

    return upstream


# Names of multipart form fields that carry reference images. The list
# is short and explicit on purpose — we don't want to ingest every
# upload, only the ones the upstream workflow actually consumes.
_REFERENCE_FILE_FIELDS = ("input_reference[]", "input_reference")


def expand_multipart_files(body: Mapping[str, Any]) -> dict[str, Any]:
    """Replace the synthetic ``__form_files__`` placeholder with
    ``ref_image_<N>`` data URLs, ready to forward upstream.

    The frontend uploads reference images as multipart file fields
    named ``input_reference[]`` (or, for compatibility, ``input_reference``).
    The upstream ComfyUI workflow expects each reference image as a
    distinct field named ``ref_image_0``, ``ref_image_1``, ... — both
    plain URLs and ``data:image/...;base64,...`` URLs are accepted.

    Mapping rules:

    * The order is the order the files appeared in the multipart form.
      ``input_reference[]`` and ``input_reference`` are concatenated in
      that order, so a single ``input_reference[]`` with 3 files becomes
      ``ref_image_0/1/2``, and mixing the two field names also
      concatenates left-to-right.
    * Each value is encoded as a ``data:<mime>;base64,...`` URL using
      the multipart file's declared ``content_type`` (falling back to
      ``image/png``). This keeps a single request self-contained — no
      external upload step is needed.
    * ``__form_files__`` is removed from the returned dict so it never
      reaches the upstream whitelist filter (it would be dropped there
      anyway, but removing it explicitly keeps the upstream body clean).
    * If the body has no ``__form_files__`` (the JSON path), the input
      is returned untouched minus the placeholder key.
    """

    if "__form_files__" not in body:
        return dict(body)

    out = {k: v for k, v in body.items() if k != "__form_files__"}
    files = body.get("__form_files__") or []
    if not isinstance(files, list):
        return out

    images: list[str] = []
    for entry in files:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("field") not in _REFERENCE_FILE_FIELDS:
            continue
        data = entry.get("data")
        if not isinstance(data, (bytes, bytearray)) or not data:
            continue
        mime = (entry.get("content_type") or "image/png").strip().lower()
        # Defence in depth: only allow obvious image/* mimes through.
        if not mime.startswith("image/"):
            mime = "image/png"
        encoded = base64.b64encode(bytes(data)).decode("ascii")
        images.append(f"data:{mime};base64,{encoded}")

    for index, data_url in enumerate(images):
        out[f"ref_image_{index}"] = data_url

    return out


def extract_result_urls(results: Any) -> list[str]:
    """Recursively extract unique HTTP(S) URLs from an AutoDL result tree."""

    found: list[str] = []
    seen: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, str):
            candidate = item.strip()
            if (
                candidate.startswith(("https://", "http://"))
                and candidate not in seen
            ):
                seen.add(candidate)
                found.append(candidate)
            return

        if isinstance(item, Mapping):
            preferred_keys = (
                "url",
                "image_url",
                "video_url",
                "audio_url",
                "download_url",
            )
            visited_keys: set[Any] = set()
            for key in preferred_keys:
                if key in item:
                    visited_keys.add(key)
                    visit(item[key])
            for key, nested in item.items():
                if key not in visited_keys:
                    visit(nested)
            return

        if isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(results)
    return found


def coerce_field_types(
    body: dict[str, Any], field_types: Mapping[str, str]
) -> dict[str, Any]:
    """Coerce client values to the upstream field types in place.

    OpenAI clients send everything as strings (form/multipart) or native
    JSON numbers. AutoDL declares integer/float types and rejects strings
    with "参数值非法". Convert ``integer`` and ``float`` fields; leave
    everything else untouched.
    """

    for key, ftype in field_types.items():
        if key not in body:
            continue
        value = body[key]
        if ftype in {"integer", "int", "long"}:
            try:
                body[key] = int(value)
            except (TypeError, ValueError):
                pass  # leave as-is; upstream will report the precise error
        elif ftype in {"float", "number", "double", "decimal"}:
            try:
                body[key] = float(value)
            except (TypeError, ValueError):
                pass
    return body


def map_size_to_resolution(
    size: str, labels: tuple[str, ...]
) -> str | None:
    """Map an OpenAI ``WxH`` size string to an AutoDL resolution label.

    OpenAI clients express size as ``"1280x720"``; AutoDL workflows expose
    labels like ``"480p竖"`` / ``"736p横"`` / ``"480p(1:1)"``. This helper
    picks the closest label by:

    1. orientation: square (w==h) → ``(1:1)``, portrait (w<h) → ``竖``,
       landscape (w>h) → ``横``;
    2. tier: among labels of that orientation, choose the one whose pixel
       tier is nearest to the short edge.

    Returns ``None`` when ``size`` is not ``WxH`` or no label matches the
    orientation.
    """

    parts = str(size).strip().lower().replace("＊", "x").split("x")
    if len(parts) != 2:
        return None
    try:
        width, height = int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None

    if width == height:
        suffix = "(1:1)"
    elif width < height:
        suffix = "竖"
    else:
        suffix = "横"

    short = min(width, height)

    # Collect candidate labels with the same orientation, parsing their
    # numeric tier (e.g. "736p竖" → 736).
    import re

    best: tuple[int, str] | None = None
    for label in labels:
        if not label.endswith(suffix):
            continue
        match = re.match(r"(\d+)p", label)
        if not match:
            continue
        tier = int(match.group(1))
        distance = abs(tier - short)
        if best is None or distance < best[0]:
            best = (distance, label)
    return best[1] if best is not None else None


def video_object(
    *,
    task_id: str,
    model: str | None,
    upstream: Mapping[str, Any],
    created_at: int,
) -> dict[str, Any]:
    """Translate an upstream payload into an OpenAI Videos object."""

    status_value = map_status(upstream.get("status"))
    urls = extract_result_urls(upstream.get("results"))
    url = urls[0] if urls else None

    obj: dict[str, Any] = {
        "id": task_id,
        "object": "video",
        "created_at": created_at,
        "model": model or "",
        "status": status_value,
        "progress": _progress_percent(upstream, status_value),
    }
    if url is not None:
        obj["url"] = url
    if "prompt" in upstream:
        obj["prompt"] = upstream["prompt"]
    if "seconds" in upstream:
        obj["seconds"] = upstream["seconds"]
    if "size" in upstream:
        obj["size"] = upstream["size"]
    # Pass through any extra fields under `metadata` so debugging stays easy
    # without leaking them into the documented top-level shape.
    extra = {
        k: v
        for k, v in upstream.items()
        if k
        not in {
            "status",
            "results",
            "progress",
            "percent",
            "percentage",
            "prompt",
            "seconds",
            "size",
        }
    }
    if extra:
        obj["metadata"] = extra
    return obj


def _progress_percent(upstream: Mapping[str, Any], status_value: str) -> int:
    for key in ("progress", "percent", "percentage"):
        value = upstream.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number <= 1:
            number *= 100
        return max(0, min(100, int(number)))
    return {
        "queued": 0,
        "in_progress": 50,
        "completed": 100,
        "failed": 100,
    }.get(status_value, 0)
