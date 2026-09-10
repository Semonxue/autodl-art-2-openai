"""Local cache of AutoDL workflow schemas, populated at startup.

Why this exists
---------------
Each AutoDL workflow declares a whitelist of valid ``resolution`` values
(via ``input_rules.resolution.options``). Without a local cache, the
gateway only learns a value is invalid after a 502 round-trip to AutoDL.

By syncing the catalog + per-workflow schemas once at startup, we can:

* answer ``GET /v1/models`` entirely from cache (no per-request upstream
  call, no token needed for the listing call);
* validate the ``size`` field in ``POST /v1/videos`` locally and reject
  unknown resolutions with a 400 that names the legal options;
* surface a ``GET /v1/workflows/{id}/schema`` endpoint so clients can
  discover legal parameters without writing scrapers.

The cache is intentionally read-only at runtime – it refreshes only when
the process restarts. Workflows change rarely; if one does, restart the
service. No file persistence, no background refreshers, no Redis.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import UpstreamError
from .upstream import UpstreamClient


logger = logging.getLogger("autodl-openai-gateway")


@dataclass(frozen=True)
class ResolutionOption:
    """One legal ``resolution`` choice, derived from the upstream schema."""

    label: str
    # The raw ``values`` dict the upstream uses to apply this preset.
    # We don't expand it – we just keep it so we can echo it back.
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowEntry:
    """Cached view of a single AutoDL workflow."""

    workflow_id: str
    name: str
    resolution_labels: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    # Every field name the workflow's input_rules declares. Requests are
    # filtered against this whitelist so unknown client-side UI fields
    # never leak upstream.
    input_fields: tuple[str, ...] = ()
    # Field name -> upstream type (integer / float / string / enum / ...).
    # Used to coerce client-supplied strings into the expected type.
    field_types: dict[str, str] = field(default_factory=dict)


class WorkflowCache:
    """In-memory read-only catalog. Populated once at startup."""

    def __init__(self) -> None:
        self._by_id: dict[str, WorkflowEntry] = {}

    # ------------------------------------------------------------- introspection

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, workflow_id: str) -> bool:
        return workflow_id in self._by_id

    def all(self) -> list[WorkflowEntry]:
        return list(self._by_id.values())

    def get(self, workflow_id: str) -> WorkflowEntry | None:
        return self._by_id.get(workflow_id)

    # ------------------------------------------------------------- validation

    def validate_resolution(
        self, workflow_id: str, requested_size: str | None
    ) -> list[str] | None:
        """Return the legal labels if ``requested_size`` is invalid for this workflow.

        ``None`` means validation cannot run (workflow unknown or has no
        resolution rule) – callers should pass through to AutoDL.
        ``[]`` means the workflow defines no resolution whitelist.
        A non-empty list is returned when the value is illegal.
        """

        entry = self._by_id.get(workflow_id)
        if entry is None or not entry.resolution_labels:
            return None  # unknown / no rule → defer to upstream
        if requested_size is None:
            return None  # client didn't specify → defer to upstream default
        return list(entry.resolution_labels) if requested_size not in entry.resolution_labels else []

    # ------------------------------------------------------------- sync

    async def sync(self, upstream: UpstreamClient, token: str) -> None:
        """Populate the cache from upstream. Errors on individual workflows
        are logged and skipped; the gateway can still run with a partial
        cache, just without local validation for the missing workflows."""

        catalog = await upstream.list_workflows(token)
        items: Iterable[dict[str, Any]] = catalog.get("list") or []

        # Phase 1: list page → collect ids we want schemas for.
        ids: list[tuple[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            workflow_id = str(item.get("uuid", "")).strip()
            name = str(item.get("name", "")).strip()
            if workflow_id:
                ids.append((workflow_id, name))

        # Phase 2: fetch schemas in parallel with bounded concurrency.
        sem = asyncio.Semaphore(8)

        async def fetch_one(workflow_id: str, name: str) -> tuple[str, WorkflowEntry]:
            async with sem:
                try:
                    schema = await upstream.get_workflow(token, workflow_id)
                except UpstreamError as exc:
                    logger.warning(
                        "schema sync failed",
                        extra={"workflow_id": workflow_id, "error": str(exc)},
                    )
                    return workflow_id, WorkflowEntry(
                        workflow_id=workflow_id, name=name
                    )

                rules = (
                    schema.get("input_rules") if isinstance(schema, dict) else None
                )
                resolution_labels: list[str] = []
                required: list[str] = []
                input_fields: list[str] = []
                field_types: dict[str, str] = {}
                if isinstance(rules, dict):
                    input_fields = list(rules.keys())
                    res_rule = rules.get("resolution")
                    if isinstance(res_rule, dict):
                        for opt in res_rule.get("options") or []:
                            if not isinstance(opt, dict):
                                continue
                            label = str(opt.get("label", "")).strip()
                            if label:
                                resolution_labels.append(label)
                    for fname, frule in rules.items():
                        if not isinstance(frule, dict):
                            continue
                        if frule.get("required"):
                            required.append(fname)
                        ftype = frule.get("type")
                        if isinstance(ftype, str) and ftype.strip():
                            field_types[fname] = ftype.strip().lower()
                return workflow_id, WorkflowEntry(
                    workflow_id=workflow_id,
                    name=name,
                    resolution_labels=tuple(resolution_labels),
                    required_fields=tuple(required),
                    input_fields=tuple(input_fields),
                    field_types=field_types,
                )

        results = await asyncio.gather(
            *(fetch_one(wid, name) for wid, name in ids),
            return_exceptions=False,
        )

        new_map: dict[str, WorkflowEntry] = {wid: entry for wid, entry in results}
        self._by_id.clear()
        self._by_id.update(new_map)
        logger.info(
            "workflow cache synced",
            extra={"count": len(self._by_id)},
        )
