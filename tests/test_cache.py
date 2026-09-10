"""Unit tests for WorkflowCache validation logic."""

from __future__ import annotations

from app.cache import WorkflowCache, WorkflowEntry


def _make_entry(
    *,
    workflow_id: str = "wf",
    name: str = "WF",
    resolution_labels: tuple[str, ...] = (),
    required_fields: tuple[str, ...] = (),
) -> WorkflowEntry:
    return WorkflowEntry(
        workflow_id=workflow_id,
        name=name,
        resolution_labels=resolution_labels,
        required_fields=required_fields,
    )


def test_empty_cache_returns_none_for_any_resolution():
    cache = WorkflowCache()
    # Unknown workflow → defer to upstream (return None)
    assert cache.validate_resolution("any", "1280x720") is None


def test_cache_with_no_resolution_rule_defers():
    cache = WorkflowCache()
    cache._by_id["wf"] = _make_entry()  # noqa: SLF001 (test-only)
    assert cache.validate_resolution("wf", "anything") is None
    assert cache.validate_resolution("wf", None) is None


def test_cache_accepts_legal_resolution():
    cache = WorkflowCache()
    cache._by_id["wf"] = _make_entry(  # noqa: SLF001
        resolution_labels=("480p横", "480p竖", "768p横", "768p竖")
    )
    # legal value → empty list (no error)
    assert cache.validate_resolution("wf", "480p横") == []
    assert cache.validate_resolution("wf", "768p竖") == []


def test_cache_rejects_illegal_resolution():
    cache = WorkflowCache()
    cache._by_id["wf"] = _make_entry(  # noqa: SLF001
        resolution_labels=("480p横", "480p竖")
    )
    legal = cache.validate_resolution("wf", "1280x720")
    assert legal == ["480p横", "480p竖"]


def test_cache_returns_a_copy_not_internal_tuple():
    """Mutating the returned list must not corrupt internal state."""

    cache = WorkflowCache()
    cache._by_id["wf"] = _make_entry(  # noqa: SLF001
        resolution_labels=("480p横", "768p横")
    )
    legal = cache.validate_resolution("wf", "nope")
    assert legal == ["480p横", "768p横"]
    legal.append("mutated")
    # Re-query should be unaffected
    assert cache.validate_resolution("wf", "nope") == ["480p横", "768p横"]


def test_cache_len_and_contains():
    cache = WorkflowCache()
    assert len(cache) == 0
    assert "wf" not in cache
    cache._by_id["wf"] = _make_entry()  # noqa: SLF001
    assert len(cache) == 1
    assert "wf" in cache
    assert cache.get("wf") is not None
    assert cache.get("missing") is None


def test_cache_all_returns_list():
    cache = WorkflowCache()
    cache._by_id["a"] = _make_entry(workflow_id="a", name="A")  # noqa: SLF001
    cache._by_id["b"] = _make_entry(workflow_id="b", name="B")  # noqa: SLF001
    all_entries = cache.all()
    assert {e.workflow_id for e in all_entries} == {"a", "b"}
