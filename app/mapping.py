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
        * A ``references`` array (top-level or carried inside
          ``metadata``) is expanded into ``ref_image_<N>`` slots
          *after* the merge, so it sees the final upstream shape.
          Already-populated ``ref_image_<N>`` slots are preserved
          (explicit wins).
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

    # Normalise every reference-image alias (``input_reference``,
    # ``image_url``, ``reference_image_url``, ``image_base64``, ...) into
    # canonical ``ref_image_<N>`` slots now that the metadata merge is
    # done. Without this step, those aliases get dropped by the whitelist
    # filter and the upstream workflow runs without any reference image.
    upstream = normalize_references(upstream)

    # Expand the canvas-style ``references`` array. Must come after
    # :func:`normalize_references` so the two helpers don't fight over
    # the same ``ref_image_<N>`` slots; ``expand_references`` honours
    # slots already populated by :func:`normalize_references`.
    upstream = expand_references(upstream)
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


# ---------------------------------------------------------------------------
# Reference-image alias normalisation
# ---------------------------------------------------------------------------
#
# AutoDL ComfyUI workflows declare exactly ``ref_image_0``, ``ref_image_1``,
# ... in their ``input_rules``. The whitelist filter in :func:`build_upstream_body`
# drops anything else, so if a client uses a different spelling for the same
# intent, the reference image disappears upstream and the generation runs
# without it — a silent failure that's hard to spot from logs.
#
# Different OpenAI-compatible frontends use different names for the same
# concept (the official OpenAI field, the SDK's helper, canvas-style
# metadata, etc.). This table lists every spelling we have observed in the
# wild. Anything matching is extracted into ``ref_image_<N>`` slots in a
# single canonical order; already-populated slots win (explicit wins).
#
# The entries are grouped:
#   * _SINGLE_KEYS — one reference image, placed into ``ref_image_0``
#     (or the next free slot if 0 is already populated).
#   * _PLURAL_KEYS — zero-or-more references; iterated in order so the
#     first element becomes ``ref_image_<next>``, the second the slot
#     after that, etc.
#   * ``references`` — handled separately by :func:`expand_references`
#     for backward compatibility and richer semantics (string-only,
#     skip empty entries, etc.).
#
# Order MATTERS: when the same value is reachable through multiple
# aliases (e.g. an SDK sends ``input_reference`` as a JSON object that
# also has an ``image_url`` field), the first alias that consumes a
# value wins; later aliases won't double-count.

_SINGLE_KEYS: tuple[str, ...] = (
    # OpenAI official single-reference field (string URL or
    # ``{"file_id": "..."}`` / ``{"image_url": "..."}`` object).
    "input_reference",
    # Single-image shorthands used by various frontends.
    "image",
    "image_url",
    "reference_image_url",
    "image_base64",
)


_PLURAL_KEYS: tuple[str, ...] = (
    # OpenAI official array form (Python SDK uses ``input_reference[]``
    # as a JSON list name; the bare ``input_references`` is also seen).
    "input_reference[]",
    "input_references",
)


def _coerce_single_reference_value(value: Any) -> str | None:
    """Reduce any single-reference value to a URL/data-URL string.

    Returns ``None`` when the value is unusable; callers must skip
    ``None`` results rather than coerce. Handles:

    * a plain URL or ``data:image/...;base64,...`` string;
    * ``{"file_id": "..."}`` (we keep it as ``file_id:...`` — the
      upstream AutoDL is happy with a non-URL token; if not, the
      upstream error is more actionable than a silent drop);
    * ``{"image_url": "..."}`` (extract the URL field);
    * ``{"url": "..."}``;
    * bytes / base64-ish dicts are NOT handled here (multipart already
      goes through :func:`expand_multipart_files` for byte payloads).
    """

    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        for key in ("image_url", "url", "file_id"):
            if key in value:
                nested = value[key]
                if isinstance(nested, str):
                    return nested.strip() or None
        return None
    return None


def normalize_references(body: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalise every reference-image alias to ``ref_image_<N>``.

    Operates on a copy of ``body`` so the caller's dict is untouched.
    Already-populated ``ref_image_<N>`` slots are preserved (the
    caller is the source of truth); aliases fill the *next* free
    slots in the order listed in :data:`_SINGLE_KEYS` and
    :data:`_PLURAL_KEYS`. The original alias keys are removed from
    the result so they never reach the whitelist filter as unknown
    fields.

    Aliases handled (in addition to those already covered by
    :func:`expand_multipart_files` and :func:`expand_references`):

    * ``input_reference`` (OpenAI official, JSON variant)
    * ``input_reference[]`` (OpenAI SDK JSON-array variant)
    * ``input_references`` (alternative plural)
    * ``image`` (OpenAI single-image shorthand)
    * ``image_url`` (URL-only shorthand)
    * ``reference_image_url`` (URL-only shorthand)
    * ``image_base64`` (data-URL shorthand)

    Anything that doesn't carry a usable string is silently dropped
    (defensive against malformed clients) — a missing reference is
    always reported by AutoDL upstream, which is more actionable than
    a 5xx from here.
    """

    out = dict(body)

    # Already-populated slots are the source of truth. Determine the
    # next free index once, so aliases fill slots 1, 2, ... even
    # when slot 0 is already occupied.
    next_index = 0
    while f"ref_image_{next_index}" in out:
        next_index += 1

    collected: list[str] = []

    # Plural aliases first — they may contain many entries and we want
    # them to occupy a contiguous block starting at ``next_index``.
    for plural_key in _PLURAL_KEYS:
        if plural_key not in out:
            continue
        raw = out.pop(plural_key)
        if not isinstance(raw, list):
            # A non-list value on a plural key is a client bug; treat
            # it like a single-value key as a best-effort recovery.
            coerced = _coerce_single_reference_value(raw)
            if coerced is not None:
                collected.append(coerced)
            continue
        for entry in raw:
            coerced = _coerce_single_reference_value(entry)
            if coerced is not None:
                collected.append(coerced)

    # Single-value aliases. Each one is a separate client-side
    # spelling; if more than one is present they all contribute (e.g.
    # ``image_url`` AND ``image_base64`` → two reference images).
    for single_key in _SINGLE_KEYS:
        if single_key not in out:
            continue
        raw = out.pop(single_key)
        coerced = _coerce_single_reference_value(raw)
        if coerced is not None:
            collected.append(coerced)

    # Stamp the collected values into ``ref_image_<next>`` slots,
    # skipping any that happen to collide with a pre-populated slot.
    for value in collected:
        while f"ref_image_{next_index}" in out:
            next_index += 1
        out[f"ref_image_{next_index}"] = value
        next_index += 1

    return out


def expand_references(body: Mapping[str, Any]) -> dict[str, Any]:
    """Translate the canvas-style ``references`` array into ``ref_image_<N>``.

    The infinite-canvas frontend (and similar OpenAI-compatible clients)
    pass reference images as a top-level JSON array of URLs under the
    key ``references`` — e.g.::

        {
            "model": "wf",
            "prompt": "...",
            "references": [
                "https://cdn.example/a.png",
                "https://cdn.example/b.png",
            ]
        }

    The upstream ComfyUI workflow, however, expects each reference
    image as a distinct field named ``ref_image_0``, ``ref_image_1``,
    .... Without this translation step the ``references`` array gets
    dropped by the input-rules whitelist filter and the workflow runs
    without any reference image — a silent failure that is hard to
    spot from logs.

    Rules:

    * Operates on a copy of the input — the original dict is untouched.
    * String entries are forwarded as-is (the upstream accepts both
      plain URLs and ``data:image/...;base64,...`` URLs).
    * Non-string entries are silently skipped — defensive against
      objects/None sneaking in from a misbehaving client.
    * ``ref_image_<N>`` slots that are *already* populated (e.g. the
      caller also uploaded files via multipart, or hand-wrote the
      fields) are left untouched — explicit wins over the convenience
      ``references`` list.
    * The original ``references`` key is removed from the returned
      dict so it never reaches the upstream whitelist filter.
    * Returns the dict unchanged when ``references`` is absent or not
      a list.
    """

    if "references" not in body:
        return dict(body)

    out = dict(body)
    raw = out.pop("references")
    if not isinstance(raw, list):
        return out

    # Find the next free slot so we don't trample on existing values
    # left by ``expand_multipart_files`` or hand-written clients.
    next_index = 0
    while f"ref_image_{next_index}" in out:
        next_index += 1

    for entry in raw:
        if not isinstance(entry, str):
            continue
        value = entry.strip()
        if not value:
            continue
        out[f"ref_image_{next_index}"] = value
        next_index += 1

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


# Keys the response summary extracts verbatim because they are
# cheap, useful, and carry no risk of being huge. Anything outside
# this set is only listed by name (``extra_keys``) — never by value —
# so we never accidentally dump a base64 thumbnail or a base64
# reference image into the log file.
_SUMMARY_KNOWN_KEYS = ("status", "progress", "percent", "percentage", "model")


def summarize_upstream_response(
    upstream: Any, *, endpoint: str
) -> dict[str, Any]:
    """Build a small, safe-to-log dict from an upstream AutoDL response.

    The gateway may run with ``LOG_UPSTREAM_RESPONSES=1`` to capture
    this digest on every upstream call. To keep the log line bounded
    and free of secrets:

    * ``status`` / ``progress`` / ``model`` are copied if short strings.
    * ``results`` is reduced to ``result_count`` (length) and
      ``result_urls`` (URL strings extracted by
      :func:`extract_result_urls`).
    * ``task_id`` is captured at the top level if present (AutoDL
      typically echoes it on submit responses).
    * Any *other* top-level key on the upstream dict is recorded by
      name only, never by value — so a future schema addition never
      silently leaks base64 or PII into the log file.
    * A non-dict upstream (shouldn't happen, but defensive) reduces
      to ``{"endpoint": ..., "type": <typename>}``.

    The result is JSON-serialisable and small — typically < 1 KB.
    """

    summary: dict[str, Any] = {"endpoint": endpoint}

    if not isinstance(upstream, Mapping):
        summary["type"] = type(upstream).__name__
        return summary

    # task_id — sometimes echoed back by submit responses.
    task_id = upstream.get("task_id")
    if isinstance(task_id, str) and task_id.strip():
        summary["task_id"] = task_id.strip()

    # Cheap scalar fields we DO want to see verbatim.
    for key in _SUMMARY_KNOWN_KEYS:
        if key not in upstream:
            continue
        value = upstream[key]
        if isinstance(value, str):
            # Trim long strings — never spill a 10 MB status message.
            summary[key] = value if len(value) <= 200 else value[:200] + "…"
        elif isinstance(value, (int, float, bool)) or value is None:
            summary[key] = value
        else:
            # Skip non-scalar values for these fields (a stray dict or
            # list here is a schema surprise; just don't print it).
            continue

    # results — count + URLs only, never the payload. Always include
    # the count (0 when absent) so log consumers can rely on the key.
    results = upstream.get("results")
    if isinstance(results, list):
        summary["result_count"] = len(results)
    elif results is None:
        summary["result_count"] = 0
    else:
        # A bare dict / scalar — count as 1 so it's still visible.
        summary["result_count"] = 1
    summary["result_urls"] = extract_result_urls(upstream.get("results"))

    # All *other* top-level keys, by name only.
    known = {"task_id", *_SUMMARY_KNOWN_KEYS, "results"}
    extras = sorted(k for k in upstream.keys() if k not in known)
    if extras:
        summary["extra_keys"] = extras

    return summary
